# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Le DDL PostgreSQL livré, exécuté sur un VRAI PostgreSQL 16 (conteneur jetable).

Ces tests sont optionnels, car ils exigent Docker et l'image `postgres:16` déjà présente sur la
machine (aucun téléchargement n'est déclenché) :

    STRIPE_TESTS_DOCKER=1 pytest tests/test_postgres_docker.py

Ils prouvent ce qu'un test SQLite ne peut pas prouver : syntaxe PostgreSQL, rôles et droits
colonne par colonne, journal d'audit en ajout seul, publication logique et décodage du journal
de transactions (WAL), c'est-à-dire le mécanisme que Debezium utilise en production.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid

import pytest

from stripe_demo import generer_ddl as g
from stripe_demo import supervision

IMAGE = "postgres:16"

pytestmark = pytest.mark.skipif(
    os.environ.get("STRIPE_TESTS_DOCKER") != "1" or shutil.which("docker") is None,
    reason="tests Docker optionnels : définir STRIPE_TESTS_DOCKER=1 (Docker, postgres:16)")


def _docker(*args: str, entree: str | None = None, verifier: bool = True):
    return subprocess.run(["docker", *args], input=entree, capture_output=True, text=True,
                          encoding="utf-8", timeout=180, check=verifier)


@pytest.fixture(scope="module")
def pg():
    """Conteneur PostgreSQL 16 jetable, non publié sur le réseau, schéma appliqué."""
    if _docker("image", "inspect", IMAGE, verifier=False).returncode != 0:
        pytest.skip(f"image {IMAGE} absente : elle n'est pas téléchargée automatiquement")
    nom = f"stripe-test-{uuid.uuid4().hex[:8]}"
    _docker("run", "-d", "--rm", "--name", nom, "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
            "-e", "POSTGRES_DB=stripe", IMAGE, "-c", "wal_level=logical",
            "-c", "max_replication_slots=4", "-c", "max_wal_senders=4")
    try:
        for _ in range(60):
            if _docker("exec", nom, "pg_isready", "-U", "postgres", "-d", "stripe",
                       verifier=False).returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("PostgreSQL ne répond pas")
        time.sleep(2)  # fin du redémarrage de l'initialisation de l'image

        def psql(sql: str, role: str | None = None, verifier: bool = True):
            if role:
                sql = f"SET ROLE {role};\n{sql}"
            return _docker("exec", "-i", nom, "psql", "-U", "postgres", "-d", "stripe", "-At",
                           "-v", "ON_ERROR_STOP=1", "-f", "-", entree=sql, verifier=verifier)

        psql(g.ddl_oltp_postgresql())
        yield psql
    finally:
        _docker("rm", "-f", nom, verifier=False)


def test_le_ddl_est_rejouable(pg):
    pg(g.ddl_oltp_postgresql())  # deuxième application : aucune erreur


def test_treize_tables_et_publication(pg):
    tables = pg("SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE';").stdout.strip()
    assert tables == "13"
    publiees = pg("SELECT string_agg(tablename, ',' ORDER BY tablename) "
                  "FROM pg_publication_tables WHERE pubname = 'stripe_cdc';").stdout.strip()
    assert publiees.split(",") == sorted(g.TABLES_CDC)


_AMORCE = """
INSERT INTO pays VALUES ('FR','France','EEE') ON CONFLICT DO NOTHING;
INSERT INTO devises VALUES ('EUR','euro',2) ON CONFLICT DO NOTHING;
INSERT INTO marchands (id_marchand, raison_sociale, code_pays, code_mcc, statut, cree_le)
    VALUES (1,'M1','FR','5411','actif',now()) ON CONFLICT DO NOTHING;
INSERT INTO clients (id_client, id_marchand, email_hache, code_pays, cree_le)
    VALUES (1,1,'h','FR',now()) ON CONFLICT DO NOTHING;
INSERT INTO moyens_paiement (id_moyen, id_client, type, jeton_psp)
    VALUES (1,1,'carte','pm_test') ON CONFLICT DO NOTHING;
"""


def test_droits_au_moindre_privilege(pg):
    pg(_AMORCE)
    pg("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='svc_test') THEN "
       "CREATE ROLE svc_test LOGIN IN ROLE role_application; END IF; END $$;")
    insertion = ("INSERT INTO paiements (id_paiement, id_marchand, id_client, id_moyen, "
                 "montant_mineur, code_devise, statut, cree_le, appareil, cle_idempotence) "
                 "VALUES (1,1,1,1,1000,'EUR','reussi',now(),'mobile','cle-1');")
    pg(insertion, role="svc_test")
    pg("UPDATE paiements SET statut = 'conteste' WHERE id_paiement = 1;", role="svc_test")
    refus_montant = pg("UPDATE paiements SET montant_mineur = 1 WHERE id_paiement = 1;",
                       role="svc_test", verifier=False)
    refus_suppression = pg("DELETE FROM paiements WHERE id_paiement = 1;", role="svc_test",
                           verifier=False)
    doublon = pg(insertion.replace("VALUES (1,", "VALUES (2,"), role="svc_test",
                 verifier=False)
    assert "permission denied" in refus_montant.stderr
    assert "permission denied" in refus_suppression.stderr
    assert "paiements_cle_idempotence_key" in doublon.stderr  # double débit refusé


def test_journal_d_audit_en_ajout_seul(pg):
    pg("INSERT INTO journal_audit (acteur, action, entite, id_entite, horodatage) "
       "VALUES ('a','b','c','d',now());")
    for instruction in ("UPDATE journal_audit SET acteur = 'x';", "DELETE FROM journal_audit;",
                        "TRUNCATE journal_audit;"):
        resultat = pg(instruction, verifier=False)
        assert resultat.returncode != 0 and "ajout seul" in resultat.stderr, instruction


def test_le_wal_publie_les_insertions_et_les_mises_a_jour(pg):
    """Décodage logique du journal : ce que Debezium lit, ligne à ligne, en production."""
    pg("SELECT pg_create_logical_replication_slot('test_slot', 'test_decoding');")
    try:
        pg(_AMORCE)
        pg("INSERT INTO paiements (id_paiement, id_marchand, id_client, id_moyen, "
           "montant_mineur, code_devise, statut, cree_le, appareil, cle_idempotence) "
           "VALUES (50,1,1,1,2500,'EUR','reussi',now(),'api','cle-wal');"
           "UPDATE paiements SET statut = 'rembourse' WHERE id_paiement = 50;")
        flux = pg("SELECT data FROM pg_logical_slot_peek_changes('test_slot', NULL, NULL) "
                  "WHERE data LIKE 'table public.paiements:%';").stdout
        assert "INSERT: id_paiement[bigint]:50" in flux
        assert "UPDATE: id_paiement[bigint]:50" in flux
        assert "statut[character varying]:'rembourse'" in flux
    finally:
        pg("SELECT pg_drop_replication_slot('test_slot');")


def test_la_requete_de_retard_signale_un_emplacement_sans_lecteur(pg):
    """La requête de `stripe_demo.supervision` (document 10) sur un vrai emplacement logique.

    L'emplacement est créé puis laissé sans lecteur, comme quand Debezium s'arrête : il retient
    le WAL produit ensuite, et la commande doit le signaler.
    """
    pg("SELECT pg_create_logical_replication_slot('test_retard', 'test_decoding');")
    try:
        pg(_AMORCE)
        pg("INSERT INTO paiements (id_paiement, id_marchand, id_client, id_moyen, "
           "montant_mineur, code_devise, statut, cree_le, appareil, cle_idempotence) "
           "VALUES (60,1,1,1,4200,'EUR','reussi',now(),'api','cle-retard');")
        ligne = pg(supervision.REQUETE_RETARD + " WHERE slot_name = 'test_retard';").stdout
        nom, actif, retard = ligne.strip().split("|")
        emplacement = supervision.Emplacement(nom, actif == "t", int(retard))
        assert emplacement.nom == "test_retard" and not emplacement.actif
        assert emplacement.retard_octets is not None and emplacement.retard_octets > 0
        assert supervision.evaluer([emplacement], 10**12) == [
            f"test_retard : aucun lecteur (connecteur arrêté), "
            f"WAL retenu : {emplacement.retard_octets} octets"]
    finally:
        pg("SELECT pg_drop_replication_slot('test_retard');")
