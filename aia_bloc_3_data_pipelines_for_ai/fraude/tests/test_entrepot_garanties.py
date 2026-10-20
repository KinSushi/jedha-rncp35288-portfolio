# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Entrepôt : tout ou rien, idempotence, bornes des lectures et refus des écritures invalides."""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.exc import ArgumentError, IntegrityError, OperationalError, ResourceClosedError

from detection_fraude.entrepot import (
    COLONNES_ALERTES,
    COLONNES_ARGENT,
    COLONNES_BRONZE,
    Entrepot,
    _par_paquets,
    construire_insertion,
)

T0 = 1_700_000_000
FORME_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


# --------------------------------------------------------------------------------------------
# Fabriques de lignes (toutes les colonnes sont fournies : la base les exige)
# --------------------------------------------------------------------------------------------
def identifiant(rang: int) -> str:
    return f"{rang:032x}"


def ligne_bronze(rang: int, **surcharges: Any) -> dict[str, Any]:
    ligne: dict[str, Any] = {
        "trans_num": identifiant(rang),
        "version_schema": 1,
        "source": "jeu",
        "horodatage_s": T0 + rang,
        "carte_hash": "c" * 32,
        "carte_fin": "1234",
        "commercant": f"Marchand {rang}",
        "categorie": "grocery_pos",
        "montant": 10.0 + rang,
        "etat": "NY",
        "population_ville": 1000,
        "age": 30,
        "distance_km": 1.5,
        "lat_commercant": 40.5,
        "long_commercant": -73.5,
        "etiquette_source": None,
        "sujet": "transactions.valides",
        "position_file": str(rang),
        "publie_le_s": 1.5,
        "recu_le": "2026-10-10T08:00:00+00:00",
        "lot_id": "lot-test",
    }
    ligne.update(surcharges)
    return ligne


def ligne_argent(rang: int, **surcharges: Any) -> dict[str, Any]:
    ligne: dict[str, Any] = {
        "trans_num": identifiant(rang),
        "date_transaction": "2026-10-10",
        "horodatage_utc": "2026-10-10T08:00:00+00:00",
        "horodatage_s": T0 + rang,
        "carte_hash": "c" * 32,
        "carte_fin": "1234",
        "commercant": f"Marchand {rang}",
        "categorie": "grocery_pos",
        "montant": 10.0 + rang,
        "etat": "NY",
        "population_ville": 1000,
        "age": 30,
        "distance_km": 1.5,
        "heure": 8,
        "jour_semaine": 5,
        "nb_tx_24h": 0,
        "montant_relatif": 1.0,
        "score": 0.05,
        "est_fraude": 0,
        "seuil": 0.5,
        "version_modele": "v1",
        "version_regles": "r1",
        "etiquette_source": None,
        "source": "jeu",
        "traite_le": "2026-10-10T08:00:01+00:00",
        "lot_id": "lot-test",
    }
    ligne.update(surcharges)
    return ligne


def ligne_alerte(rang: int, **surcharges: Any) -> dict[str, Any]:
    ligne: dict[str, Any] = {
        "trans_num": identifiant(rang),
        "date_transaction": "2026-10-10",
        "horodatage_utc": "2026-10-10T08:00:00+00:00",
        "carte_fin": "1234",
        "commercant": f"Marchand {rang}",
        "categorie": "shopping_net",
        "montant": 900.0,
        "score": 0.95,
        "seuil": 0.5,
        "version_modele": "v1",
        "source": "jeu",
        "statut_notification": "a_envoyer",
        "tentatives": 0,
        "derniere_tentative": None,
        "notifiee_le": None,
        "latence_notification_s": None,
        "canaux_ok": "",
        "cree_le": "2026-10-10T08:00:00+00:00",
        "detectee_le_s": 1000.0 + rang,
    }
    ligne.update(surcharges)
    return ligne


def autre_valeur(valeur: Any) -> Any:
    """Une valeur différente de la même nature (pour vérifier qu'une colonne a bien changé)."""
    if valeur is None:
        return 1
    if isinstance(valeur, int):
        return valeur + 1
    if isinstance(valeur, float):
        return valeur + 0.5
    return f"{valeur}-bis"


def ecrire_lignage(entrepot: Entrepot, etape: str, **options: Any) -> None:
    entrepot.enregistrer_lignage(
        etape,
        "entree",
        "sortie",
        nb_entrees=1,
        nb_sorties=1,
        debut="2026-10-10T08:00:00+00:00",
        version_regles="r",
        **options,
    )


def mettre_bronze(entrepot: Entrepot, *lignes: dict[str, Any]) -> None:
    with entrepot.transaction() as conn:
        entrepot.inserer_bronze(conn, list(lignes))


def mettre_argent(entrepot: Entrepot, *lignes: dict[str, Any], remplacer: bool = False) -> None:
    with entrepot.transaction() as conn:
        entrepot.inserer_argent(conn, list(lignes), remplacer=remplacer)


def mettre_alertes(entrepot: Entrepot, *lignes: dict[str, Any], remplacer: bool = False) -> None:
    with entrepot.transaction() as conn:
        entrepot.inserer_alertes(conn, list(lignes), remplacer=remplacer)


def contenu(entrepot: Entrepot, table: str) -> list[dict[str, Any]]:
    """Instantané complet d'une table, trié sur sa première colonne."""
    return entrepot.requete(f"SELECT * FROM {table} ORDER BY 1, 2")


def trois_couches(entrepot: Entrepot) -> list[list[dict[str, Any]]]:
    tables = ("bronze_transactions", "argent_transactions", "or_alertes_fraude")
    return [contenu(entrepot, table) for table in tables]


class ConnexionEspion:
    """Enveloppe une connexion et note chaque exécution (les paramètres reçus)."""

    def __init__(self, vraie: Any = None) -> None:
        self.vraie = vraie
        self.executions: list[Any] = []

    def execute(self, instruction: Any, parametres: Any = None) -> Any:
        self.executions.append(parametres)
        return self.vraie.execute(instruction, parametres)


# --------------------------------------------------------------------------------------------
# Fonctions pures
# --------------------------------------------------------------------------------------------
def test_construire_insertion_ignore_ou_remplace_et_ne_modifie_pas_ses_arguments():
    """Sans liste de remplacement le doublon part ; sinon chaque colonne est mise à jour."""
    colonnes, remplacer = ("id", "a", "b"), ["a", "b"]
    colonnes_avant, remplacer_avant = tuple(colonnes), list(remplacer)
    assert construire_insertion("ma_table", colonnes, "id") == (
        "INSERT INTO ma_table (id, a, b) VALUES (:id, :a, :b) ON CONFLICT (id) DO NOTHING"
    )
    assert construire_insertion("ma_table", colonnes, "id", remplacer) == (
        "INSERT INTO ma_table (id, a, b) VALUES (:id, :a, :b) ON CONFLICT (id) "
        "DO UPDATE SET a = excluded.a, b = excluded.b"
    )
    assert construire_insertion("t", ("x", "y"), "x, y", ["y"]) == (  # clé composite (meta_rejets)
        "INSERT INTO t (x, y) VALUES (:x, :y) ON CONFLICT (x, y) DO UPDATE SET y = excluded.y"
    )
    assert (colonnes, remplacer) == (colonnes_avant, remplacer_avant)


def test_par_paquets_decoupe_sans_perdre_ni_dupliquer_et_refuse_une_taille_nulle():
    """Les identifiants partent par paquets de 500 au plus ; une taille nulle est refusée."""
    valeurs = list(range(1001))
    valeurs_avant = list(valeurs)
    assert [len(p) for p in _par_paquets(valeurs)] == [500, 500, 1]
    assert [len(p) for p in _par_paquets(valeurs[:500])] == [500]
    assert [len(p) for p in _par_paquets(valeurs[:501])] == [500, 1]
    assert [len(p) for p in _par_paquets([])] == []
    assert [list(p) for p in _par_paquets(list(range(7)), 3)] == [[0, 1, 2], [3, 4, 5], [6]]
    assert [
        x for p in _par_paquets(valeurs) for x in p
    ] == valeurs_avant  # rien perdu, rien en double
    assert valeurs == valeurs_avant
    with pytest.raises(ValueError) as exc:
        list(_par_paquets([1], 0))
    assert str(exc.value) == "range() arg 3 must not be zero"


# --------------------------------------------------------------------------------------------
# Construction, réglages, cycle de vie
# --------------------------------------------------------------------------------------------
def test_un_entrepot_sqlite_cree_le_dossier_manquant_regle_ses_connexions_et_se_ferme(tmp_path):
    """Dossier créé, WAL, attente de 30 s, synchro normale, pool vérifié ; fichier bloquant."""
    fichier = tmp_path / "a" / "b" / "entrepot.db"
    assert not fichier.parent.exists()
    base = Entrepot(f"sqlite:///{fichier}")
    try:
        assert base.dialecte == "sqlite"
        assert fichier.parent.is_dir()
        base.initialiser()
        reglages = (
            base.scalaire("PRAGMA busy_timeout"),
            base.scalaire("PRAGMA journal_mode"),
            base.scalaire("PRAGMA synchronous"),
        )
        assert reglages == (30000, "wal", 1)
        assert base.moteur.pool.checkedin() == 1  # la connexion rendue est gardée dans le pool
        verifications = []
        ping_reel = base.moteur.dialect.do_ping
        base.moteur.dialect.do_ping = lambda c: verifications.append(1) or ping_reel(c)
        base.requete("SELECT 1")
        assert verifications == [1]  # une connexion reprise du pool est testée avant usage
        mettre_bronze(base, ligne_bronze(1))
    finally:
        base.fermer()
    assert base.moteur.pool.checkedin() == 0  # fermer() libère les connexions du pool
    rouvert = Entrepot(f"sqlite:///{fichier}")  # le dossier existe déjà : aucune erreur
    try:
        assert rouvert.scalaire("SELECT COUNT(*) FROM bronze_transactions") == 1  # mêmes données
    finally:
        rouvert.fermer()
    # Un fichier à la place du dossier : la construction échoue et ne touche pas à ce fichier.
    bloquant = tmp_path / "bloquant"
    contenu_initial = "contenu initial"
    bloquant.write_text(contenu_initial, encoding="utf-8")
    with pytest.raises(FileExistsError):
        Entrepot(f"sqlite:///{bloquant / 'entrepot.db'}")
    assert bloquant.read_text(encoding="utf-8") == contenu_initial


@pytest.mark.parametrize("dsn", ["sqlite://", "sqlite:///:memory:"])
def test_un_entrepot_en_memoire_fonctionne_sans_creer_ni_dossier_ni_fichier(
    dsn, tmp_path, monkeypatch
):
    """Une base en mémoire n'a pas de fichier : rien n'est créé sur le disque."""
    monkeypatch.chdir(tmp_path)
    contenu_avant = sorted(p.name for p in tmp_path.iterdir())
    base = Entrepot(dsn)
    try:
        base.initialiser()
        assert base.scalaire("SELECT COUNT(*) FROM bronze_transactions") == 0
    finally:
        base.fermer()
    assert sorted(p.name for p in tmp_path.iterdir()) == contenu_avant


def test_un_entrepot_distant_ne_cree_aucun_dossier_local_et_ne_se_connecte_pas(
    tmp_path, monkeypatch
):
    """Un SGBD distant (PostgreSQL) n'a ni fichier ni réglage SQLite et ne se connecte pas."""
    monkeypatch.chdir(tmp_path)
    contenu_avant = sorted(p.name for p in tmp_path.iterdir())
    base = Entrepot("postgresql+psycopg2://u:p@127.0.0.1:1/base")
    try:
        assert base.dialecte == "postgresql"
        assert base.moteur.pool.checkedin() == 0
    finally:
        base.fermer()
    assert sorted(p.name for p in tmp_path.iterdir()) == contenu_avant


def test_un_dsn_illisible_est_refuse_et_ne_cree_rien(tmp_path, monkeypatch):
    """Une chaîne de connexion invalide échoue tout de suite, sans laisser de dossier."""
    monkeypatch.chdir(tmp_path)
    contenu_avant = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(ArgumentError):
        Entrepot("pas-un-dsn")
    assert sorted(p.name for p in tmp_path.iterdir()) == contenu_avant


def test_initialiser_laisse_les_donnees_intactes_et_cree_les_tables_et_index_attendus(entrepot):
    """Rejouer l'initialisation ne vide rien ; les colonnes des tables suivent les constantes."""
    mettre_bronze(entrepot, ligne_bronze(1))
    mettre_argent(entrepot, ligne_argent(1))
    mettre_alertes(entrepot, ligne_alerte(1))
    couches_avant = trois_couches(entrepot)
    entrepot.initialiser()
    entrepot.initialiser()
    assert trois_couches(entrepot) == couches_avant
    objets = entrepot.requete(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    )
    tables = {o["name"] for o in objets if o["type"] == "table"}
    index = {o["name"] for o in objets if o["type"] == "index"}
    assert tables == {
        "bronze_transactions",
        "argent_transactions",
        "or_alertes_fraude",
        "or_rapport_quotidien",
        "or_rapport_categorie",
        "meta_rejets",
        "meta_lignage",
    }
    assert index == {
        "idx_bronze_carte",
        "idx_bronze_temps",
        "idx_argent_jour",
        "idx_alertes_statut",
        "idx_alertes_jour",
        "idx_rejets_jour",
    }
    for table, colonnes in (
        ("bronze_transactions", COLONNES_BRONZE),
        ("argent_transactions", COLONNES_ARGENT),
        ("or_alertes_fraude", COLONNES_ALERTES),
    ):
        declarees = [c["name"] for c in entrepot.requete(f"PRAGMA table_info({table})")]
        assert declarees == list(colonnes)


# --------------------------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------------------------
def test_une_panne_au_milieu_d_une_transaction_ne_laisse_aucune_ecriture(entrepot):
    """Bronze, argent et alertes sont écrits ensemble ou pas du tout (atomicité du lot)."""
    mettre_bronze(entrepot, ligne_bronze(1))
    couches_avant = trois_couches(entrepot)
    with pytest.raises(RuntimeError) as exc, entrepot.transaction() as conn:
        entrepot.inserer_bronze(conn, [ligne_bronze(2)])
        entrepot.inserer_argent(conn, [ligne_argent(2)])
        entrepot.inserer_alertes(conn, [ligne_alerte(2)])
        raise RuntimeError("panne au milieu du lot")
    assert str(exc.value) == "panne au milieu du lot"
    assert trois_couches(entrepot) == couches_avant


def test_une_interruption_clavier_dans_une_transaction_annule_aussi_les_ecritures(entrepot):
    """Même un arrêt demandé par l'opérateur (BaseException) ne laisse pas de lot à moitié."""
    couches_avant = trois_couches(entrepot)
    with pytest.raises(KeyboardInterrupt), entrepot.transaction() as conn:
        entrepot.inserer_bronze(conn, [ligne_bronze(1)])
        raise KeyboardInterrupt
    assert trois_couches(entrepot) == couches_avant


def test_les_autres_lecteurs_ne_voient_rien_tant_que_la_transaction_n_est_pas_validee(entrepot):
    """Une écriture n'est visible qu'au commit : pendant la transaction, la base n'a pas bougé."""
    compte_avant = entrepot.scalaire("SELECT COUNT(*) FROM bronze_transactions")
    with entrepot.transaction() as conn:
        entrepot.inserer_bronze(conn, [ligne_bronze(1)])
        compte_pendant = entrepot.scalaire("SELECT COUNT(*) FROM bronze_transactions")
        dans_la_transaction = conn.exec_driver_sql("SELECT COUNT(*) FROM bronze_transactions")
        assert dans_la_transaction.scalar() == 1  # l'auteur, lui, voit sa propre écriture
    assert compte_pendant == compte_avant
    assert entrepot.scalaire("SELECT COUNT(*) FROM bronze_transactions") == 1


def test_une_ligne_incomplete_est_refusee_par_la_base_et_annule_tout_le_paquet(entrepot):
    """Une colonne obligatoire vide fait échouer le paquet entier : la ligne valide n'entre pas."""
    couches_avant = trois_couches(entrepot)
    with pytest.raises(IntegrityError) as exc, entrepot.transaction() as conn:
        entrepot.inserer_bronze(conn, [ligne_bronze(1), ligne_bronze(2, version_schema=None)])
    assert exc.value.orig.args == (
        "NOT NULL constraint failed: bronze_transactions.version_schema",
    )
    assert trois_couches(entrepot) == couches_avant


# --------------------------------------------------------------------------------------------
# Lecture générique
# --------------------------------------------------------------------------------------------
def test_requete_et_scalaire_lient_les_parametres_sans_les_interpreter(entrepot):
    """Un paramètre qui ressemble à du SQL reste une simple valeur : la base n'est pas touchée."""
    mettre_bronze(entrepot, ligne_bronze(1))
    couches_avant = trois_couches(entrepot)
    piege = "x'; DROP TABLE bronze_transactions; --"
    assert entrepot.requete("SELECT :a AS a, :b AS b", a=1, b=piege) == [{"a": 1, "b": piege}]
    assert entrepot.scalaire("SELECT :valeur", valeur=piege) == piege
    assert entrepot.requete("SELECT 1 AS un WHERE 0") == []
    assert entrepot.scalaire("SELECT 1 WHERE 0") is None
    assert entrepot.scalaire("SELECT 7, 8") == 7  # première colonne de la première ligne
    assert entrepot.requete("SELECT 1 AS a, 2 AS b") == [{"a": 1, "b": 2}]
    assert trois_couches(entrepot) == couches_avant


def test_la_lecture_generique_ne_peut_rien_ecrire_dans_la_base(entrepot):
    """``requete`` et ``scalaire`` ne valident jamais : une suppression passée par eux saute."""
    mettre_bronze(entrepot, ligne_bronze(1), ligne_bronze(2))
    couches_avant = trois_couches(entrepot)
    with pytest.raises(ResourceClosedError):
        entrepot.requete("DELETE FROM bronze_transactions")
    with pytest.raises(ResourceClosedError):
        entrepot.scalaire("UPDATE bronze_transactions SET montant = 0")
    assert trois_couches(entrepot) == couches_avant


# --------------------------------------------------------------------------------------------
# Identifiants existants
# --------------------------------------------------------------------------------------------
def test_identifiants_existants_ne_renvoie_que_les_presents_et_n_ecrit_rien(entrepot):
    """Doublons et inconnus sont ignorés ; n'importe quel itérable est accepté."""
    mettre_bronze(entrepot, ligne_bronze(1), ligne_bronze(2))
    couches_avant = trois_couches(entrepot)
    demandes = [identifiant(n) for n in (2, 1, 3, 1, 3, 2, 99)]
    with entrepot.transaction() as conn:
        trouves = entrepot.identifiants_existants(conn, demandes)
        depuis_un_generateur = entrepot.identifiants_existants(conn, (i for i in demandes))
        aucun = entrepot.identifiants_existants(conn, [])
    assert trouves == {identifiant(1), identifiant(2)}
    assert depuis_un_generateur == trouves
    assert aucun == set()
    assert isinstance(trouves, set)
    assert trois_couches(entrepot) == couches_avant


@pytest.mark.parametrize(
    ("combien", "tailles"),
    [(0, []), (1, [1]), (499, [499]), (500, [500]), (501, [500, 1]), (1001, [500, 500, 1])],
)
def test_identifiants_existants_interroge_par_paquets_de_500_et_laisse_la_base_inchangee(
    entrepot, combien, tailles
):
    """La clause IN reste bornée (500 valeurs) quel que soit le nombre d'identifiants demandés."""
    couches_avant = trois_couches(entrepot)
    with entrepot.transaction() as conn:
        espion = ConnexionEspion(conn)
        trouves = entrepot.identifiants_existants(espion, [identifiant(n) for n in range(combien)])
    assert [len(p["ids"]) for p in espion.executions] == tailles
    assert trouves == set()
    assert trois_couches(entrepot) == couches_avant


def test_identifiants_existants_dedoublonne_avant_de_decouper_en_paquets(entrepot):
    """Mille fois le même identifiant ne fait qu'une requête d'un seul identifiant."""
    couches_avant = trois_couches(entrepot)
    with entrepot.transaction() as conn:
        espion = ConnexionEspion(conn)
        entrepot.identifiants_existants(espion, [identifiant(5)] * 1000 + [identifiant(6)] * 3)
    assert [p["ids"] for p in espion.executions] == [[identifiant(5), identifiant(6)]]
    assert trois_couches(entrepot) == couches_avant


def test_identifiants_existants_retrouve_les_presents_de_part_et_d_autre_des_paquets(entrepot):
    """Les présents placés aux frontières des paquets (499/500, 999/1000) sont tous retrouvés."""
    presents = [0, 499, 500, 999, 1000, 1199]
    mettre_bronze(entrepot, *(ligne_bronze(n) for n in presents))
    couches_avant = trois_couches(entrepot)
    with entrepot.transaction() as conn:
        trouves = entrepot.identifiants_existants(conn, [identifiant(n) for n in range(1200)])
    assert trouves == {identifiant(n) for n in presents}
    assert trois_couches(entrepot) == couches_avant


def test_identifiants_existants_cherche_dans_la_table_demandee_et_refuse_une_table_inconnue(
    entrepot,
):
    """Par défaut le bronze ; avec ``table=``, l'argent ; une table absente fait échouer."""
    mettre_bronze(entrepot, ligne_bronze(1))
    mettre_argent(entrepot, ligne_argent(2))
    couches_avant = trois_couches(entrepot)
    with entrepot.transaction() as conn:
        dans_bronze = entrepot.identifiants_existants(conn, [identifiant(1), identifiant(2)])
        dans_argent = entrepot.identifiants_existants(
            conn, [identifiant(1), identifiant(2)], "argent_transactions"
        )
    assert dans_bronze == {identifiant(1)}
    assert dans_argent == {identifiant(2)}
    with pytest.raises(OperationalError) as exc, entrepot.transaction() as conn:
        entrepot.identifiants_existants(conn, [identifiant(1)], "table_inconnue")
    assert exc.value.orig.args == ("no such table: table_inconnue",)
    assert trois_couches(entrepot) == couches_avant


# --------------------------------------------------------------------------------------------
# Écritures bronze / argent / alertes
# --------------------------------------------------------------------------------------------
def test_inserer_des_listes_vides_est_sans_effet_et_n_execute_aucune_requete(entrepot):
    """Un lot sans nouveauté (tout en doublon) n'envoie aucune instruction à la base."""
    couches_avant = trois_couches(entrepot)
    espion = ConnexionEspion()  # sans connexion réelle : toute exécution ferait échouer le test
    entrepot.inserer_bronze(espion, [])
    entrepot.inserer_argent(espion, [])
    entrepot.inserer_argent(espion, [], remplacer=True)
    entrepot.inserer_alertes(espion, [])
    entrepot.inserer_alertes(espion, [], remplacer=True)
    assert espion.executions == []
    assert trois_couches(entrepot) == couches_avant


def test_un_doublon_de_bronze_laisse_la_ligne_originale_inchangee(entrepot):
    """Le bronze est immuable : réécrire un ``trans_num`` ne change rien, les nouveaux entrent."""
    ligne_original = ligne_bronze(1, montant=10.0)
    mettre_bronze(entrepot, ligne_original)
    nouvelle = ligne_bronze(2)
    mettre_bronze(
        entrepot, ligne_bronze(1, montant=999.0, commercant="Autre", lot_id="autre"), nouvelle
    )
    lignes = contenu(entrepot, "bronze_transactions")
    assert lignes == [ligne_original, nouvelle]  # complet : chaque colonne est écrite et relue


def test_argent_sans_remplacement_ignore_un_doublon_et_laisse_la_ligne_inchangee(entrepot):
    """Rejouer une transaction déjà notée ne remplace pas sa première notation."""
    ligne_original = ligne_argent(1)
    mettre_argent(entrepot, ligne_original)
    mettre_argent(entrepot, ligne_argent(1, score=0.99, est_fraude=1, version_modele="v2"))
    assert contenu(entrepot, "argent_transactions") == [ligne_original]


def test_argent_avec_remplacement_change_toutes_les_colonnes_sauf_la_cle_et_rien_d_autre(entrepot):
    """Le retraitement remplace chaque colonne de la ligne visée, sans toucher aux autres."""
    visee, voisine = ligne_argent(1), ligne_argent(2)
    mettre_argent(entrepot, visee, voisine)
    voisine_avant = entrepot.requete(
        "SELECT * FROM argent_transactions WHERE trans_num = :t", t=voisine["trans_num"]
    )
    variante = {c: autre_valeur(v) for c, v in visee.items() if c != "trans_num"}
    assert all(variante[c] != visee[c] for c in variante)  # garde-fou : tout a bien changé
    mettre_argent(entrepot, {"trans_num": visee["trans_num"], **variante}, remplacer=True)
    assert entrepot.requete(
        "SELECT * FROM argent_transactions WHERE trans_num = :t", t=visee["trans_num"]
    ) == [{"trans_num": visee["trans_num"], **variante}]
    assert (
        entrepot.requete(
            "SELECT * FROM argent_transactions WHERE trans_num = :t", t=voisine["trans_num"]
        )
        == voisine_avant
    )
    assert entrepot.scalaire("SELECT COUNT(*) FROM argent_transactions") == 2


def test_alertes_sans_remplacement_ignore_un_doublon_et_laisse_l_alerte_inchangee(entrepot):
    """Rejouer une détection ne recrée pas l'alerte, donc ne déclenche pas de second envoi."""
    alerte_initiale = ligne_alerte(1, statut_notification="envoyee", tentatives=1)
    mettre_alertes(entrepot, alerte_initiale)
    mettre_alertes(entrepot, ligne_alerte(1, statut_notification="a_envoyer", score=0.1))
    assert contenu(entrepot, "or_alertes_fraude") == [alerte_initiale]


def test_alertes_avec_remplacement_corrige_la_decision_mais_jamais_l_etat_d_envoi(entrepot):
    """Un retraitement met à jour le score et le modèle, jamais le statut ni les tentatives."""
    envoi = {
        "statut_notification": "envoyee",
        "tentatives": 3,
        "derniere_tentative": "2026-10-10T08:05:00+00:00",
        "notifiee_le": "2026-10-10T08:05:00+00:00",
        "latence_notification_s": 4.5,
        "canaux_ok": "fichier,journal",
        "cree_le": "2026-10-10T08:00:00+00:00",
        "detectee_le_s": 1234.5,
    }
    decision = {
        "date_transaction": "2026-10-11",
        "horodatage_utc": "2026-10-11T09:00:00+00:00",
        "carte_fin": "9999",
        "commercant": "Autre commerçant",
        "categorie": "misc_net",
        "montant": 777.0,
        "score": 0.61,
        "seuil": 0.4,
        "version_modele": "v2",
        "source": "api",
    }
    mettre_alertes(entrepot, ligne_alerte(1, **envoi))
    envoi_avant = {k: contenu(entrepot, "or_alertes_fraude")[0][k] for k in envoi}
    nouvelle = ligne_alerte(
        1,
        **decision,
        statut_notification="a_envoyer",
        tentatives=0,
        derniere_tentative=None,
        notifiee_le=None,
        latence_notification_s=None,
        canaux_ok="",
        cree_le="2030-01-01T00:00:00+00:00",
        detectee_le_s=1.0,
    )
    mettre_alertes(entrepot, nouvelle, remplacer=True)
    (apres,) = contenu(entrepot, "or_alertes_fraude")
    assert {k: apres[k] for k in envoi} == envoi_avant  # l'envoi n'a pas bougé
    assert {k: apres[k] for k in decision} == decision  # la décision a été remplacée
    assert apres["trans_num"] == identifiant(1)


# --------------------------------------------------------------------------------------------
# Lectures bornées
# --------------------------------------------------------------------------------------------
def test_historique_d_une_carte_est_strictement_anterieur_a_la_carte_seule_et_trie(entrepot):
    """Seul le passé strict de la bonne carte est lu : même seconde et futur exclus, tri stable."""
    a, b = "a" * 32, "b" * 32
    lignes = [
        ligne_bronze(5, carte_hash=a, horodatage_s=T0 + 10, montant=5.0),
        ligne_bronze(
            2, carte_hash=a, horodatage_s=T0 + 10, montant=2.0
        ),  # même seconde, id plus petit
        ligne_bronze(3, carte_hash=a, horodatage_s=T0 + 19, montant=7.0),
        ligne_bronze(
            4, carte_hash=a, horodatage_s=T0 + 20, montant=8.0
        ),  # pile à la borne : exclue
        ligne_bronze(6, carte_hash=a, horodatage_s=T0 + 30, montant=9.0),  # futur : exclue
        ligne_bronze(7, carte_hash=b, horodatage_s=T0 + 11, montant=1000.0),  # autre carte
    ]
    mettre_bronze(entrepot, *lignes)
    bronze_avant = contenu(entrepot, "bronze_transactions")
    historique = entrepot.historique_carte(a, T0 + 20)
    assert historique == [(T0 + 10, 2.0), (T0 + 10, 5.0), (T0 + 19, 7.0)]
    assert [(type(t), type(m)) for t, m in historique] == [(int, float)] * 3
    assert entrepot.historique_carte(a, T0 + 10) == []  # rien avant la plus ancienne
    assert entrepot.historique_carte("inconnue", T0 + 99) == []
    assert contenu(entrepot, "bronze_transactions") == bronze_avant


def test_historique_d_une_carte_avec_depuis_inclut_la_borne_basse_et_reste_strict_en_haut(
    entrepot,
):
    """Avec ``depuis_s`` la fenêtre est [depuis, avant[ : borne basse incluse, haute exclue."""
    a = "a" * 32
    mettre_bronze(
        entrepot,
        *(
            ligne_bronze(n, carte_hash=a, horodatage_s=T0 + n, montant=float(n))
            for n in range(1, 7)
        ),
    )
    bronze_avant = contenu(entrepot, "bronze_transactions")
    assert entrepot.historique_carte(a, T0 + 5, T0 + 3) == [(T0 + 3, 3.0), (T0 + 4, 4.0)]
    assert entrepot.historique_carte(a, T0 + 5, T0 + 4) == [(T0 + 4, 4.0)]
    assert entrepot.historique_carte(a, T0 + 5, T0 + 5) == []  # depuis == avant : fenêtre vide
    assert entrepot.historique_carte(a, T0 + 5, T0 + 6) == []  # depuis > avant : fenêtre vide
    assert entrepot.historique_carte(a, T0 + 5, 0) == [(T0 + n, float(n)) for n in range(1, 5)]
    assert contenu(entrepot, "bronze_transactions") == bronze_avant


def test_lire_bronze_est_semi_ouvert_trie_complet_et_ne_modifie_rien(entrepot):
    """Le bronze d'un intervalle [début, fin[ : début inclus, fin exclue, colonnes complètes."""
    lignes = [ligne_bronze(n, horodatage_s=T0 + t) for n, t in ((9, 99), (8, 100), (7, 100))]
    lignes += [ligne_bronze(6, horodatage_s=T0 + 150), ligne_bronze(5, horodatage_s=T0 + 199)]
    lignes += [ligne_bronze(4, horodatage_s=T0 + 200), ligne_bronze(3, horodatage_s=T0 + 201)]
    mettre_bronze(entrepot, *lignes)
    bronze_avant = contenu(entrepot, "bronze_transactions")
    lues = entrepot.lire_bronze(T0 + 100, T0 + 200)
    # Même seconde (100) : identifiant croissant (7 avant 8) ; 99, 200 et 201 sont exclues.
    assert lues == [lignes[2], lignes[1], lignes[3], lignes[4]]
    assert entrepot.lire_bronze(T0 + 200, T0 + 200) == []
    assert entrepot.lire_bronze(T0 + 300, T0 + 100) == []
    assert [x["horodatage_s"] for x in entrepot.lire_bronze(T0 + 199, T0 + 201)] == [
        T0 + 199,
        T0 + 200,
    ]
    assert contenu(entrepot, "bronze_transactions") == bronze_avant


def test_transactions_du_jour_ne_renvoie_que_ce_jour_dans_l_ordre_du_temps(entrepot):
    """Un rapport quotidien ne lit que sa date, dans l'ordre chronologique puis par identifiant."""
    lignes = [
        ligne_argent(3, date_transaction="2026-10-10", horodatage_s=T0 + 50),
        ligne_argent(2, date_transaction="2026-10-10", horodatage_s=T0 + 50),
        ligne_argent(1, date_transaction="2026-10-10", horodatage_s=T0 + 10),
        ligne_argent(4, date_transaction="2026-10-09", horodatage_s=T0 + 5),
        ligne_argent(5, date_transaction="2026-10-11", horodatage_s=T0 + 60),
    ]
    mettre_argent(entrepot, *lignes)
    argent_avant = contenu(entrepot, "argent_transactions")
    assert entrepot.transactions_du_jour("2026-10-10") == [lignes[2], lignes[1], lignes[0]]
    assert entrepot.transactions_du_jour("2026-10-12") == []
    assert contenu(entrepot, "argent_transactions") == argent_avant


# --------------------------------------------------------------------------------------------
# Boîte d'envoi
# --------------------------------------------------------------------------------------------
def test_seules_les_alertes_a_envoyer_sous_le_plafond_de_tentatives_sont_selectionnees(entrepot):
    """Ni les envoyées, ni les abandonnées, ni celles qui ont atteint le plafond ne repartent."""
    mettre_alertes(
        entrepot,
        ligne_alerte(1, statut_notification="a_envoyer", tentatives=0),
        ligne_alerte(2, statut_notification="a_envoyer", tentatives=2),  # sous le plafond de 3
        ligne_alerte(3, statut_notification="a_envoyer", tentatives=3),  # pile au plafond : exclue
        ligne_alerte(4, statut_notification="a_envoyer", tentatives=4),
        ligne_alerte(5, statut_notification="envoyee", tentatives=1),
        ligne_alerte(6, statut_notification="echec", tentatives=1),
    )
    alertes_avant = contenu(entrepot, "or_alertes_fraude")
    selection = entrepot.alertes_a_envoyer(3)
    assert [a["trans_num"] for a in selection] == [identifiant(1), identifiant(2)]
    assert [a["trans_num"] for a in entrepot.alertes_a_envoyer(4)] == [
        identifiant(1),
        identifiant(2),
        identifiant(3),
    ]
    assert entrepot.alertes_a_envoyer(0) == []
    assert set(selection[0]) == set(COLONNES_ALERTES)  # la ligne complète est renvoyée
    assert contenu(entrepot, "or_alertes_fraude") == alertes_avant


def test_les_alertes_a_envoyer_partent_de_la_plus_ancienne_avec_une_limite_de_100(entrepot):
    """Ordre de détection (puis identifiant) ; 100 alertes par tour par défaut, limite réglable."""
    ordre_voulu = [identifiant(n) for n in range(101)]
    # Les 101 alertes sont créées à l'envers : la lecture ne dépend pas de l'ordre d'insertion.
    mettre_alertes(
        entrepot, *(ligne_alerte(n, detectee_le_s=float(n)) for n in range(100, -1, -1))
    )
    mettre_alertes(entrepot, ligne_alerte(500, detectee_le_s=0.0))  # même instant que n°0
    alertes_avant = contenu(entrepot, "or_alertes_fraude")
    par_defaut = [a["trans_num"] for a in entrepot.alertes_a_envoyer(5)]
    assert par_defaut == [identifiant(0), identifiant(500)] + ordre_voulu[1:99]
    assert [a["trans_num"] for a in entrepot.alertes_a_envoyer(5, limite=3)] == [
        identifiant(0),
        identifiant(500),
        identifiant(1),
    ]
    assert entrepot.alertes_a_envoyer(5, 0) == []
    assert contenu(entrepot, "or_alertes_fraude") == alertes_avant


def test_marquer_une_alerte_envoyee_date_l_envoi_et_ne_touche_pas_aux_autres(entrepot):
    """Statut, tentatives, latence et canaux sont écrits ; l'envoi est daté de la tentative."""
    mettre_alertes(entrepot, ligne_alerte(1), ligne_alerte(2))
    voisine_avant = entrepot.requete(
        "SELECT * FROM or_alertes_fraude WHERE trans_num = :t", t=identifiant(2)
    )
    avant = datetime.now(UTC).replace(microsecond=0)
    entrepot.marquer_alerte(
        identifiant(1),
        statut="envoyee",
        tentatives=2,
        canaux_ok=["fichier", "journal"],
        latence_s=1.5,
    )
    apres = datetime.now(UTC)
    (alerte,) = entrepot.requete(
        "SELECT * FROM or_alertes_fraude WHERE trans_num = :t", t=identifiant(1)
    )
    assert FORME_ISO.match(alerte["derniere_tentative"])
    assert avant <= datetime.fromisoformat(alerte["derniere_tentative"]) <= apres
    assert alerte["notifiee_le"] == alerte["derniere_tentative"]
    assert (alerte["statut_notification"], alerte["tentatives"]) == ("envoyee", 2)
    assert (alerte["latence_notification_s"], alerte["canaux_ok"]) == (1.5, "fichier,journal")
    assert (
        entrepot.requete("SELECT * FROM or_alertes_fraude WHERE trans_num = :t", t=identifiant(2))
        == voisine_avant
    )


@pytest.mark.parametrize("statut", ["a_envoyer", "echec", "Envoyee", "envoyée", ""])
def test_un_statut_autre_qu_envoyee_ne_date_jamais_l_envoi(entrepot, statut):
    """Seul le statut exact « envoyee » date l'envoi ; des canaux vides donnent un texte vide."""
    mettre_alertes(entrepot, ligne_alerte(1), ligne_alerte(2))
    voisine_avant = entrepot.requete(
        "SELECT * FROM or_alertes_fraude WHERE trans_num = :t", t=identifiant(2)
    )
    entrepot.marquer_alerte(
        identifiant(1), statut=statut, tentatives=4, canaux_ok=[], latence_s=None
    )
    (alerte,) = entrepot.requete(
        "SELECT * FROM or_alertes_fraude WHERE trans_num = :t", t=identifiant(1)
    )
    assert alerte["notifiee_le"] is None
    assert alerte["latence_notification_s"] is None
    assert alerte["canaux_ok"] == ""  # texte vide, pas NULL
    assert FORME_ISO.match(alerte["derniere_tentative"])  # la tentative, elle, est datée
    assert (alerte["statut_notification"], alerte["tentatives"]) == (statut, 4)
    assert (
        entrepot.requete("SELECT * FROM or_alertes_fraude WHERE trans_num = :t", t=identifiant(2))
        == voisine_avant
    )


def test_marquer_une_alerte_inconnue_ne_cree_rien_et_ne_modifie_aucune_alerte(entrepot):
    """Un identifiant inconnu est sans effet : aucune ligne fantôme, aucune alerte touchée."""
    mettre_alertes(entrepot, ligne_alerte(1), ligne_alerte(2))
    alertes_avant = contenu(entrepot, "or_alertes_fraude")
    entrepot.marquer_alerte(
        identifiant(404), statut="envoyee", tentatives=1, canaux_ok=["x"], latence_s=2.0
    )
    assert contenu(entrepot, "or_alertes_fraude") == alertes_avant


# --------------------------------------------------------------------------------------------
# Méta : rejets et lignage
# --------------------------------------------------------------------------------------------
def test_un_rejet_est_archive_avec_ses_valeurs_par_defaut_et_en_json_lisible(entrepot):
    """Étape et raison manquantes valent « inconnue » ; JSON lisible ; le rejet reçu est intact."""
    rejet_vide: dict = {}
    rejet_rempli = {
        "etape": 5,
        "raison": "montant_trop_élevé",
        "trans_num": "t" * 32,
        "erreurs": [{"champ": "montant", "code": "hors_bornes", "detail": "é"}],
        "charge_utile_masquee": {"champs_conserves": {"categorie": "épicerie"}},
    }
    rejet_rempli_avant = copy.deepcopy(rejet_rempli)
    with entrepot.transaction() as conn:
        entrepot.enregistrer_rejet(conn, rejet_vide, "7", "transactions.rejets", "2026-10-10")
        entrepot.enregistrer_rejet(conn, rejet_rempli, "8", "transactions.rejets", "2026-10-09")
    assert rejet_rempli == rejet_rempli_avant and rejet_vide == {}
    vide, rempli = entrepot.requete("SELECT * FROM meta_rejets ORDER BY cle_message")
    recus = [vide.pop("recu_le"), rempli.pop("recu_le")]
    assert all(FORME_ISO.match(recu) for recu in recus)
    assert vide == {
        "sujet": "transactions.rejets",
        "cle_message": "7",
        "etape": "inconnue",
        "raison": "inconnue",
        "trans_num": None,
        "erreurs": "[]",
        "charge_utile": "{}",
        "date_evenement": "2026-10-10",
    }
    assert rempli == {
        "sujet": "transactions.rejets",
        "cle_message": "8",
        "etape": "5",  # converti en texte
        "raison": "montant_trop_élevé",
        "trans_num": "t" * 32,
        "erreurs": '[{"champ": "montant", "code": "hors_bornes", "detail": "é"}]',
        "charge_utile": '{"champs_conserves": {"categorie": "épicerie"}}',
        "date_evenement": "2026-10-09",
    }


def test_un_rejet_rejoue_laisse_la_premiere_archive_inchangee(entrepot):
    """Même (sujet, clé) : la première archive reste ; un autre sujet fait une autre archive."""
    with entrepot.transaction() as conn:
        entrepot.enregistrer_rejet(
            conn,
            {"etape": "producteur", "raison": "schema_invalide"},
            "1",
            "sujet-a",
            "2026-10-10",
        )
    archive_initiale = entrepot.requete("SELECT * FROM meta_rejets")
    with entrepot.transaction() as conn:
        entrepot.enregistrer_rejet(
            conn, {"etape": "consommateur", "raison": "autre"}, "1", "sujet-a", "2030-01-01"
        )
    assert entrepot.requete("SELECT * FROM meta_rejets") == archive_initiale
    with entrepot.transaction() as conn:
        entrepot.enregistrer_rejet(conn, {"raison": "autre sujet"}, "1", "sujet-b", "2026-10-10")
    rejets = entrepot.requete("SELECT sujet, cle_message, raison FROM meta_rejets ORDER BY sujet")
    assert rejets == [
        {"sujet": "sujet-a", "cle_message": "1", "raison": "schema_invalide"},
        {"sujet": "sujet-b", "cle_message": "1", "raison": "autre sujet"},
    ]


def test_le_lignage_ecrit_avec_une_connexion_disparait_si_la_transaction_est_annulee(entrepot):
    """Avec ``conn=``, le lignage suit le sort du lot ; sans, il est validé tout de suite."""
    lignage_avant = contenu(entrepot, "meta_lignage")
    with pytest.raises(RuntimeError), entrepot.transaction() as conn:
        ecrire_lignage(entrepot, "annule", conn=conn)
        raise RuntimeError("lot annulé")
    assert contenu(entrepot, "meta_lignage") == lignage_avant
    with entrepot.transaction() as conn:
        ecrire_lignage(entrepot, "valide", conn=conn)
        pendant = entrepot.scalaire("SELECT COUNT(*) FROM meta_lignage")
    assert pendant == 0  # invisible aux autres tant que le lot n'est pas validé
    assert entrepot.scalaire("SELECT COUNT(*) FROM meta_lignage") == 1
    ecrire_lignage(entrepot, "propre-transaction")  # sans connexion : sa propre transaction
    etapes = [ligne["etape"] for ligne in contenu(entrepot, "meta_lignage")]
    assert sorted(etapes) == ["propre-transaction", "valide"]


def test_le_lignage_porte_ses_valeurs_par_defaut_un_identifiant_unique_et_des_details_en_json(
    entrepot, tmp_path
):
    """Rejets 0, modèle absent par défaut ; détails sérialisés, ceux passés restent intacts."""
    debut = "2026-10-10T08:00:00+00:00"
    details = {"é": 1, "chemin": tmp_path / "x", "liste": [1, 2]}
    details_avant = copy.deepcopy(details)
    entrepot.enregistrer_lignage(
        "seul", "entree", "sortie", nb_entrees=3, nb_sorties=2, debut=debut, version_regles="r1"
    )
    entrepot.enregistrer_lignage(
        "complet", "e2", "s2", nb_entrees=10, nb_sorties=8, nb_rejets=2, debut=debut,
        version_regles="r2", version_modele="m1", details=details,
    )  # fmt: skip
    entrepot.enregistrer_lignage(
        "vide", "e3", "s3", nb_entrees=0, nb_sorties=0, debut=debut, version_regles="r3",
        details={},
    )  # fmt: skip
    assert details == details_avant
    lignes = {ligne["etape"]: ligne for ligne in entrepot.requete("SELECT * FROM meta_lignage")}
    seul, complet, vide = lignes["seul"], lignes["complet"], lignes["vide"]
    identifiants = [seul["id"], complet["id"], vide["id"]]
    assert sorted(set(identifiants)) == sorted(identifiants)  # trois identifiants distincts
    assert all(re.fullmatch(r"[0-9a-f]{32}", x["id"]) for x in (seul, complet, vide))
    assert all(FORME_ISO.match(x["fin"]) for x in (seul, complet, vide))
    assert seul["debut"] == debut
    assert (seul["nb_rejets"], seul["version_modele"], seul["details"]) == (0, None, "{}")
    assert (seul["entree"], seul["sortie"], seul["version_regles"]) == ("entree", "sortie", "r1")
    assert (complet["nb_entrees"], complet["nb_sorties"], complet["nb_rejets"]) == (10, 8, 2)
    assert (complet["version_regles"], complet["version_modele"]) == ("r2", "m1")
    assert complet["details"] == json.dumps(
        {"é": 1, "chemin": str(tmp_path / "x"), "liste": [1, 2]}, ensure_ascii=False
    )
    assert vide["details"] == "{}"


# --------------------------------------------------------------------------------------------
# Cycle de vie : purge
# --------------------------------------------------------------------------------------------
def test_la_purge_est_stricte_ne_supprime_que_le_bronze_et_renvoie_le_nombre_supprime(entrepot):
    """Réception strictement avant la date : supprimée ; pile à la date ou après : conservée."""
    limite = "2026-06-01T00:00:00+00:00"
    mettre_bronze(
        entrepot,
        ligne_bronze(1, recu_le="2026-01-01T00:00:00+00:00"),
        ligne_bronze(2, recu_le="2026-05-31T23:59:59+00:00"),
        ligne_bronze(3, recu_le=limite),  # pile à la limite : conservée
        ligne_bronze(4, recu_le="2026-06-01T00:00:01+00:00"),
    )
    mettre_argent(entrepot, ligne_argent(1), ligne_argent(2))
    mettre_alertes(entrepot, ligne_alerte(1))
    argent_avant = contenu(entrepot, "argent_transactions")
    alertes_avant = contenu(entrepot, "or_alertes_fraude")
    assert entrepot.purger_bronze(limite) == 2
    restants = [b["trans_num"] for b in contenu(entrepot, "bronze_transactions")]
    assert restants == [identifiant(3), identifiant(4)]
    assert contenu(entrepot, "argent_transactions") == argent_avant
    assert contenu(entrepot, "or_alertes_fraude") == alertes_avant
    bronze_apres_premiere_purge = contenu(entrepot, "bronze_transactions")
    assert entrepot.purger_bronze(limite) == 0  # rejouée : plus rien à supprimer
    assert contenu(entrepot, "bronze_transactions") == bronze_apres_premiere_purge


def test_purger_avec_une_date_anterieure_a_tout_ne_supprime_rien_et_renvoie_zero_entier(entrepot):
    """Aucune ligne assez ancienne : zéro suppression, valeur renvoyée de type entier."""
    mettre_bronze(entrepot, ligne_bronze(1), ligne_bronze(2))
    bronze_avant = contenu(entrepot, "bronze_transactions")
    supprimees = entrepot.purger_bronze("2000-01-01T00:00:00+00:00")
    assert supprimees == 0 and isinstance(supprimees, int)
    assert contenu(entrepot, "bronze_transactions") == bronze_avant
    assert entrepot.purger_bronze("2100-01-01T00:00:00+00:00") == 2
    assert contenu(entrepot, "bronze_transactions") == []


# --------------------------------------------------------------------------------------------
# Texte exact des requêtes (relevé par la mutation : la casse des mots-clés SQL ne se voit pas
# dans les résultats, mais le même texte part en production vers PostgreSQL)
# --------------------------------------------------------------------------------------------
def test_lire_bronze_envoie_la_requete_documentee(entrepot, instructions_sql):
    """Intervalle semi-ouvert sur l'horodatage, tri chronologique puis par identifiant."""
    entrepot.lire_bronze(T0, T0 + 10)
    assert instructions_sql == [
        "SELECT * FROM bronze_transactions "
        "WHERE horodatage_s >= ? AND horodatage_s < ? "
        "ORDER BY horodatage_s, trans_num"
    ]


def test_transactions_du_jour_envoie_la_requete_documentee(entrepot, instructions_sql):
    """Une seule requête : le jour demandé, trié chronologiquement puis par identifiant."""
    entrepot.transactions_du_jour("2026-10-10")
    assert instructions_sql == [
        "SELECT * FROM argent_transactions WHERE date_transaction = ? "
        "ORDER BY horodatage_s, trans_num"
    ]


def test_archiver_un_rejet_envoie_l_insertion_documentee_et_ignore_le_conflit(
    entrepot, instructions_sql
):
    """Table, colonnes et clé de conflit (sujet, clé du message) sont ceux de la documentation."""
    with entrepot.transaction() as conn:
        entrepot.enregistrer_rejet(conn, {}, "7", "transactions.rejets", "2026-10-10")
    assert instructions_sql == [
        "INSERT INTO meta_rejets (sujet, cle_message, etape, raison, trans_num, erreurs, "
        "charge_utile, date_evenement, recu_le) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (sujet, cle_message) DO NOTHING"
    ]


def test_purger_le_bronze_envoie_la_suppression_documentee(entrepot, instructions_sql):
    """Une seule suppression, bornée par la date de réception."""
    entrepot.purger_bronze("2026-10-10T00:00:00+00:00")
    assert instructions_sql == ["DELETE FROM bronze_transactions WHERE recu_le < ?"]


def insertion_attendue(table: str, colonnes: tuple[str, ...], cle: str, fin: str) -> str:
    """Le texte d'un INSERT portable, écrit avec la table, la clé et la clause donnés en clair."""
    noms = ", ".join(colonnes)
    valeurs = ", ".join("?" for _ in colonnes)
    return f"INSERT INTO {table} ({noms}) VALUES ({valeurs}) ON CONFLICT ({cle}) {fin}"


def mise_a_jour_attendue(colonnes: tuple[str, ...]) -> str:
    return "DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in colonnes)


def test_chaque_insertion_envoie_sa_table_ses_colonnes_et_sa_clause_de_conflit(
    entrepot, instructions_sql
):
    """Bronze et argent visent trans_num ; en retraitement l'argent remplace tout sauf la clé."""
    with entrepot.transaction() as conn:
        entrepot.inserer_bronze(conn, [ligne_bronze(1)])
        entrepot.inserer_argent(conn, [ligne_argent(1)])
        entrepot.inserer_argent(conn, [ligne_argent(1)], remplacer=True)
        entrepot.inserer_alertes(conn, [ligne_alerte(1)])
        entrepot.inserer_alertes(conn, [ligne_alerte(1)], remplacer=True)
    remplacees_en_argent = tuple(c for c in COLONNES_ARGENT if c != "trans_num")
    remplacees_en_alerte = (
        "date_transaction",
        "horodatage_utc",
        "carte_fin",
        "commercant",
        "categorie",
        "montant",
        "score",
        "seuil",
        "version_modele",
        "source",
    )
    assert instructions_sql == [
        insertion_attendue("bronze_transactions", COLONNES_BRONZE, "trans_num", "DO NOTHING"),
        insertion_attendue("argent_transactions", COLONNES_ARGENT, "trans_num", "DO NOTHING"),
        insertion_attendue(
            "argent_transactions",
            COLONNES_ARGENT,
            "trans_num",
            mise_a_jour_attendue(remplacees_en_argent),
        ),
        insertion_attendue("or_alertes_fraude", COLONNES_ALERTES, "trans_num", "DO NOTHING"),
        insertion_attendue(
            "or_alertes_fraude",
            COLONNES_ALERTES,
            "trans_num",
            mise_a_jour_attendue(remplacees_en_alerte),
        ),
    ]


def test_identifiants_existants_interroge_par_defaut_la_table_bronze(entrepot, instructions_sql):
    """Sans table précisée, c'est le bronze qui est interrogé, avec une clause IN."""
    with entrepot.transaction() as conn:
        entrepot.identifiants_existants(conn, [identifiant(1), identifiant(2)])
    assert instructions_sql == [
        "SELECT trans_num FROM bronze_transactions WHERE trans_num IN (?, ?)"
    ]


def test_historique_d_une_carte_envoie_la_requete_documentee_avec_ou_sans_borne_basse(
    entrepot, instructions_sql
):
    """La borne basse n'ajoute que sa clause ; le tri est toujours chronologique."""
    entrepot.historique_carte("c" * 32, T0)
    entrepot.historique_carte("c" * 32, T0, T0 - 100)
    debut = "SELECT horodatage_s, montant FROM bronze_transactions "
    filtre = "WHERE carte_hash = ? AND horodatage_s < ? "
    tri = "ORDER BY horodatage_s, trans_num"
    assert instructions_sql == [
        debut + filtre + tri,
        debut + filtre + "AND horodatage_s >= ? " + tri,
    ]


def test_les_alertes_a_envoyer_sont_choisies_par_la_requete_documentee(entrepot, instructions_sql):
    """Statut « a_envoyer », sous le plafond de tentatives, de la plus ancienne, avec limite."""
    entrepot.alertes_a_envoyer(3)
    assert instructions_sql == [
        "SELECT * FROM or_alertes_fraude WHERE statut_notification = 'a_envoyer' "
        "AND tentatives < ? ORDER BY detectee_le_s, trans_num LIMIT ?"
    ]


def test_marquer_une_alerte_envoie_la_mise_a_jour_documentee(entrepot, instructions_sql):
    """Une seule mise à jour : statut, tentatives, dates, latence, canaux ; ciblée sur l'alerte."""
    entrepot.marquer_alerte(
        identifiant(1), statut="envoyee", tentatives=1, canaux_ok=["a"], latence_s=0.5
    )
    assert instructions_sql == [
        "UPDATE or_alertes_fraude SET statut_notification = ?, tentatives = ?, "
        "derniere_tentative = ?, notifiee_le = ?, latence_notification_s = ?, "
        "canaux_ok = ? WHERE trans_num = ?"
    ]


def test_le_lignage_envoie_l_insertion_documentee(entrepot, instructions_sql):
    """Douze colonnes, dans l'ordre du modèle de lignage."""
    ecrire_lignage(entrepot, "etape-test")
    assert instructions_sql == [
        "INSERT INTO meta_lignage (id, etape, entree, sortie, nb_entrees, nb_sorties, "
        "nb_rejets, debut, fin, version_regles, version_modele, details) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ]


def test_chaque_connexion_sqlite_recoit_ses_trois_reglages_dans_l_ordre(tmp_path):
    """Attente de 30 s sur verrou, journal WAL, synchro normale : écrits tels quels."""
    base = Entrepot(f"sqlite:///{tmp_path / 'entrepot.db'}")
    vues: list[str] = []

    def tracer(connexion, enregistrement):
        connexion.set_trace_callback(vues.append)

    event.listen(base.moteur, "connect", tracer, insert=True)  # avant les réglages de l'entrepôt
    try:
        base.scalaire("SELECT 1")
    finally:
        base.fermer()
    # « read_uncommitted » est le réglage propre à SQLAlchemy, pas celui de l'entrepôt.
    reglages = [
        vue for vue in vues if vue.lower().startswith("pragma") and "read_uncommitted" not in vue
    ]
    assert reglages == [
        "PRAGMA busy_timeout=30000",
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
    ]
