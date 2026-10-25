# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Entrepôt en étoile : connexion, ordre des instructions du chargement et reprise sur erreur.

`test_olap.py` et `test_cdc.py` prouvent que les FAITS et les AGRÉGATS sont justes. Ici, on prouve
la mécanique des chargeurs : l'entrepôt est créé tel que décrit, les instructions partent dans
l'ordre voulu (vider, recharger, agréger, valider, détacher), et surtout qu'une erreur au milieu
d'un chargement n'efface rien et ne laisse pas la base OLTP attachée : sans cela, tous les
micro-lots suivants échoueraient (« database with name oltp already exists ») jusqu'au
redémarrage du processus, et un chemin erroné laisserait un entrepôt vide.
"""

from __future__ import annotations

import duckdb
import pytest
from aide_demo import ConnexionEspion
from sqlalchemy import text

from stripe_demo import olap, oltp

TABLES = ("dim_date", "dim_marchand", "dim_client", "dim_moyen", "dim_geographie",
          "faits_paiements")


def _compter(cx, table: str) -> int:
    return cx.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0]


def _etat(cx) -> dict[str, int]:
    return {table: _compter(cx, table) for table in TABLES}


def _bases_attachees(cx) -> set[str]:
    return {ligne[0] for ligne in cx.execute("SELECT database_name FROM duckdb_databases()")
            .fetchall()}


def test_connecter_cree_les_six_tables_conserve_les_donnees_et_refuse_un_doublon(tmp_path):
    chemin = tmp_path / "entrepot.duckdb"

    cx = olap.connecter(str(chemin))
    cx.execute("INSERT INTO dim_geographie VALUES ('FR', 'France', 'EEE')")
    cx.close()

    assert chemin.is_file()
    cx = duckdb.connect(str(chemin), read_only=True)
    noms = {ligne[0] for ligne in cx.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()}
    cx.close()
    assert noms == set(TABLES)

    cx = olap.connecter(str(chemin))  # `CREATE TABLE IF NOT EXISTS` : rien n'est écrasé
    assert cx.execute("SELECT code_pays, nom FROM dim_geographie").fetchall() == [
        ("FR", "France")]
    with pytest.raises(duckdb.ConstraintException):  # les clés primaires refusent un doublon
        cx.execute("INSERT INTO dim_geographie VALUES ('FR', 'Autre', 'EEE')")
    assert _compter(cx, "dim_geographie") == 1


def test_le_chargement_batch_et_la_reprise_des_changements_suivent_l_ordre_voulu(mini_demo,
                                                                                tmp_path):
    espion = ConnexionEspion(mini_demo.entrepot)
    avant = _etat(mini_demo.entrepot)

    resultat = olap.charger_batch(espion, str(mini_demo.chemin_oltp))

    textes = espion.textes()
    assert textes[:3] == [
        "INSTALL sqlite; LOAD sqlite;",
        f"ATTACH '{mini_demo.chemin_oltp}' AS oltp (TYPE sqlite, READ_ONLY)",
        "begin()"]
    assert textes[3:9] == [f"DELETE FROM {t}" for t in (
        "faits_paiements", "dim_date", "dim_marchand", "dim_client", "dim_moyen",
        "dim_geographie")]
    assert textes[9].startswith("INSERT INTO dim_geographie SELECT code_pays, nom, region")
    assert textes[10].startswith("INSERT INTO dim_client SELECT c.id_client, c.code_pays,")
    assert textes[11] == "SELECT count(*) FROM faits_paiements"
    assert textes[12].startswith("INSERT INTO dim_date SELECT")
    assert "FROM oltp.paiements p WHERE TRUE)" in textes[12]
    assert textes[13].startswith("INSERT OR REPLACE INTO faits_paiements SELECT")
    assert textes[13].endswith("WHERE TRUE")
    assert textes[14] == "SELECT count(*) FROM faits_paiements"
    assert textes[15].startswith("CREATE OR REPLACE TABLE agr_chiffre_quotidien AS")
    assert textes[16:] == ["commit()", "DETACH oltp"]
    assert resultat == {"faits": _compter(mini_demo.entrepot, "faits_paiements")}
    assert _etat(mini_demo.entrepot) == avant  # rejoué : le même entrepôt

    # Reprise : les changements d'après la position, puis rien quand il n'y en a plus.
    mini_demo.ajouter_paiements(20, graine=9300)
    espion = ConnexionEspion(mini_demo.entrepot)
    position, nombre = olap.appliquer_changements(
        espion, str(mini_demo.chemin_oltp), mini_demo.position)
    textes = espion.textes()
    assert textes[:3] == [
        "INSTALL sqlite; LOAD sqlite;",
        f"ATTACH '{mini_demo.chemin_oltp}' AS oltp (TYPE sqlite, READ_ONLY)",
        "begin()"]
    assert espion.instructions[3] == (
        "SELECT coalesce(max(position), ?) FROM oltp.journal_changements", (mini_demo.position,))
    assert (position, nombre) == (mini_demo.position + 20, 20)
    filtre = ("p.id_paiement IN (SELECT id_ligne FROM oltp.journal_changements "
              f"WHERE table_source = 'paiements' AND position > {mini_demo.position} "
              f"AND position <= {position})")
    assert textes[4] == "SELECT count(*) FROM faits_paiements"
    assert f"WHERE {filtre})" in textes[5]  # dim_date : jours nouveaux des paiements concernés
    assert textes[6].endswith(f"WHERE {filtre}")
    assert textes[7] == "SELECT count(*) FROM faits_paiements"
    assert textes[8].startswith("CREATE OR REPLACE TABLE agr_chiffre_quotidien AS")
    assert textes[9:] == ["commit()", "DETACH oltp"]
    assert olap.appliquer_changements(
        mini_demo.entrepot, str(mini_demo.chemin_oltp), position) == (position, 0)

    # Un journal vide rend la position reçue sans rien charger.
    moteur = oltp.moteur(str(tmp_path / "oltp.sqlite"))
    oltp.creer(moteur)
    oltp.referentiels(moteur, jours=3)
    entrepot = olap.connecter(str(tmp_path / "entrepot.duckdb"))
    olap.charger_batch(entrepot, str(tmp_path / "oltp.sqlite"))
    assert olap.appliquer_changements(entrepot, str(tmp_path / "oltp.sqlite"), 123) == (123, 0)
    assert _compter(entrepot, "faits_paiements") == 0
    moteur.dispose()


def test_inserer_faits_ne_compte_que_les_lignes_nouvelles_et_un_filtre_vide_n_ajoute_rien(
        mini_demo):
    cx = mini_demo.entrepot
    cx.execute("INSTALL sqlite; LOAD sqlite;")
    cx.execute(f"ATTACH '{mini_demo.chemin_oltp}' AS oltp (TYPE sqlite, READ_ONLY)")
    try:
        cx.execute("DELETE FROM faits_paiements WHERE id_paiement IN (2, 4, 6)")
        total_apres_suppression = _compter(cx, "faits_paiements")

        nouveaux = olap.inserer_faits(cx, "p.id_paiement <= 10")
        rejoues = olap.inserer_faits(cx, "p.id_paiement <= 10")
        avant = _etat(cx)
        aucun = olap.inserer_faits(cx, "FALSE")
    finally:
        cx.execute("DETACH oltp")

    assert nouveaux == 3
    assert rejoues == 0  # les 10 faits existent : ils sont remplacés, pas ajoutés
    assert _compter(cx, "faits_paiements") == total_apres_suppression + 3
    assert aucun == 0
    assert _etat(cx) == avant


def test_une_erreur_n_efface_rien_et_detache_toujours_la_base_oltp(mini_demo, tmp_path,
                                                                    monkeypatch):
    """Régression : un chemin erroné vidait l'entrepôt, une erreur laissait `oltp` attachée."""
    avant = _etat(mini_demo.entrepot)

    # Un chemin OLTP introuvable : refusé, et l'entrepôt déjà chargé reste intact.
    with pytest.raises(duckdb.Error):
        olap.charger_batch(mini_demo.entrepot, str(tmp_path / "absent.sqlite"))
    assert _etat(mini_demo.entrepot) == avant
    assert "oltp" not in _bases_attachees(mini_demo.entrepot)

    # Une position qui n'est pas un nombre : jamais interprétée comme du SQL.
    mini_demo.ajouter_paiements(15, graine=9400)
    for position in ("0; DROP TABLE faits_paiements", "pas un nombre"):
        with pytest.raises((ValueError, duckdb.Error)):
            olap.appliquer_changements(mini_demo.entrepot, str(mini_demo.chemin_oltp), position)
        assert _etat(mini_demo.entrepot) == avant
        assert "oltp" not in _bases_attachees(mini_demo.entrepot)
    # L'appel suivant, correct, réussit : rien n'est resté attaché.
    position, nombre = olap.appliquer_changements(
        mini_demo.entrepot, str(mini_demo.chemin_oltp), mini_demo.position)
    assert (position, nombre) == (mini_demo.position + 15, 15)

    # Une panne au milieu du chargement batch : tout est annulé, la base est détachée.
    avant_panne = _etat(mini_demo.entrepot)

    def echec(*_arguments):
        raise duckdb.Error("panne simulée pendant le calcul des faits")

    monkeypatch.setattr(olap, "inserer_faits", echec)
    with pytest.raises(duckdb.Error, match="panne simulée"):
        olap.charger_batch(mini_demo.entrepot, str(mini_demo.chemin_oltp))
    monkeypatch.undo()
    assert _etat(mini_demo.entrepot) == avant_panne
    assert "oltp" not in _bases_attachees(mini_demo.entrepot)
    olap.charger_batch(mini_demo.entrepot, str(mini_demo.chemin_oltp))  # on peut recharger

    # Un fichier SQLite qui n'est pas un OLTP de la démonstration (sans journal) est refusé.
    autre = tmp_path / "autre.sqlite"
    moteur = oltp.moteur(str(autre))
    with moteur.begin() as cx:
        cx.execute(text("CREATE TABLE autre (id INTEGER)"))
    moteur.dispose()
    entrepot = olap.connecter(str(tmp_path / "vide.duckdb"))
    with pytest.raises(duckdb.Error):
        olap.appliquer_changements(entrepot, str(autre), 0)
    with pytest.raises(duckdb.Error):
        olap.charger_batch(entrepot, str(autre))
