# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Mesures réelles du dimensionnement : PostgreSQL, démonstration, qualité des alertes, commande.

Les mesures publiées dans `livrables/aia2/donnees/` alimentent la note de dimensionnement ; ces
tests vérifient que le code qui les produit calcule ce qu'il annonce. PostgreSQL n'étant pas
disponible, `create_engine` est remplacé par un faux moteur qui répond à chaque requête exacte
par une valeur connue (une requête inattendue fait échouer le test) : on vérifie ainsi les
instructions envoyées ET le calcul fait des réponses. La partie « démonstration » s'appuie sur
une vraie exécution en petit (voir `aide_demo.py`). Chaque fonction a ses refus : requête
inattendue, données sans étiquette, fichier manquant, commande inconnue.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
import numpy as np
import pandas as pd
import pytest
from aide_demo import (
    PAS_D_HORLOGE,
    ConnexionEspion,
    executer_en_petit,
    faux_entrainer,
    noter_les_appels_json,
    noter_les_requetes_sqlalchemy,
)

from stripe_demo import fraude, mesures, olap, pipeline

MOMENT = datetime(2026, 8, 1, tzinfo=UTC)


# ---------------------------------------------------------------- faux moteur PostgreSQL


class FauxResultat:
    def __init__(self, valeur):
        self.valeur = valeur

    def scalar_one(self):
        return self.valeur

    def one(self):
        return self.valeur

    def __iter__(self):
        return iter(self.valeur)


class FausseConnexion:
    def __init__(self, moteur, options=None):
        self.moteur = moteur
        self.options = options or {}

    def __enter__(self):
        return self

    def __exit__(self, *erreur):
        return False

    def execution_options(self, **options):
        self.moteur.options_demandees.append(options)
        return FausseConnexion(self.moteur, options)

    def execute(self, requete, parametres=None):
        sql = " ".join(str(requete).split())
        self.moteur.requetes.append((sql, parametres, self.options.get("isolation_level")))
        reponse = self.moteur.reponses[sql]
        return FauxResultat(reponse(parametres) if callable(reponse) else reponse)


class FauxMoteur:
    """Répond à chaque requête exacte par une valeur connue ; note tout ce qu'on lui demande."""

    TABLES = ("abonnements", "paiements", "pays")

    def __init__(self):
        self.requetes: list[tuple] = []
        self.options_demandees: list[dict] = []
        tailles = {"abonnements": (0, 8192, 8192, 16384), "paiements": (48000, 6168576,
                                                                          8126464, 14327808),
                   "pays": (8, 8192, 16384, 57344)}
        self.reponses = {
            "SELECT pg_current_wal_lsn()": "0/1000000",
            "VACUUM (ANALYZE)": None,
            "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), :d)": 31_022_552,
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename": [
                (nom,) for nom in self.TABLES],
            "SHOW server_version": "16.15 (Debian 16.15-1.pgdg13+2)",
        }
        self.tailles_par_table = tailles
        for nom in tailles:
            self.reponses[f"SELECT count(*) FROM {nom}"] = (
                lambda _parametres, nom=nom: self.tailles_par_table[nom][0])
        self.reponses[
            "SELECT pg_relation_size(CAST(:t AS regclass)), "
            "pg_indexes_size(CAST(:t AS regclass)), "
            "pg_total_relation_size(CAST(:t AS regclass))"] = self._tailles

    def _tailles(self, parametres):
        _lignes, tas, index, total = self.tailles_par_table[parametres["t"]]
        return (tas, index, total)

    def connect(self):
        return FausseConnexion(self)


@pytest.fixture
def postgres_factice(monkeypatch):
    """Remplace create_engine, la génération des données et l'horloge ; rend le faux moteur."""
    moteur = FauxMoteur()
    creations, referentiels, lots = [], [], []
    monkeypatch.setattr(mesures, "create_engine", lambda url: creations.append(url) or moteur)
    monkeypatch.setattr(mesures.oltp, "referentiels",
                        lambda m, jours: referentiels.append((m, jours)))

    def faux_lot(m, debut, nombre, graine, premier_id):
        lots.append((m, debut, nombre, graine, premier_id))
        return premier_id + nombre - 1

    monkeypatch.setattr(mesures.oltp, "paiements_synthetiques", faux_lot)
    lectures = {"n": 0}

    def horloge():
        lectures["n"] += 1
        return lectures["n"] * PAS_D_HORLOGE

    monkeypatch.setattr(mesures.time, "perf_counter", horloge)
    moteur.creations, moteur.referentiels, moteur.lots = creations, referentiels, lots
    return moteur


# ---------------------------------------------------------------- mesures PostgreSQL


def test_les_mesures_postgres_chargent_les_donnees_et_lisent_les_tailles_table_par_table(
        postgres_factice):
    resultat = mesures.mesurer_postgres("postgresql+psycopg://u@h:5432/b", jours=3, par_jour=10)

    # Le moteur est créé avec l'URL donnée ; référentiels des jours plus deux, un lot par jour.
    assert postgres_factice.creations == ["postgresql+psycopg://u@h:5432/b"]
    assert postgres_factice.referentiels == [(postgres_factice, 5)]
    assert postgres_factice.lots == [
        (postgres_factice, MOMENT, 10, 1000, 1),
        (postgres_factice, MOMENT + timedelta(days=1), 10, 1001, 11),
        (postgres_factice, MOMENT + timedelta(days=2), 10, 1002, 21)]

    # Le résultat : serveur, paiements, jours, durée (arrondie), WAL, tables.
    assert list(resultat) == ["serveur", "paiements", "jours", "duree_chargement_s",
                              "wal_octets", "tables"]
    assert resultat["serveur"] == "16.15 (Debian 16.15-1.pgdg13+2)"
    assert resultat["paiements"] == 30
    assert resultat["jours"] == 3
    assert resultat["duree_chargement_s"] == 0.1  # 0,123456 s arrondi à 1 décimale
    assert resultat["wal_octets"] == 31_022_552
    tables = resultat["tables"]
    assert list(tables) == ["abonnements", "paiements", "pays"]
    assert tables["paiements"] == {
        "lignes": 48000, "tas_octets": 6168576, "index_octets": 8126464,
        "total_octets": 14327808, "octets_par_ligne": 298.5}
    assert all(isinstance(v, int) for k, v in tables["paiements"].items() if k.endswith("octets"))
    assert tables["pays"]["octets_par_ligne"] == 7168.0
    assert tables["abonnements"] == {  # une table vide n'a pas de taille par ligne
        "lignes": 0, "tas_octets": 8192, "index_octets": 8192, "total_octets": 16384,
        "octets_par_ligne": None}

    # Les instructions : position du journal d'abord, VACUUM en validation automatique avant la
    # lecture de la position finale, tailles demandées table par table avec le nom en paramètre.
    requetes = [(sql, parametres) for sql, parametres, _ in postgres_factice.requetes]
    assert requetes[0] == ("SELECT pg_current_wal_lsn()", None)
    assert ("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), :d)", {"d": "0/1000000"}) in requetes
    assert postgres_factice.options_demandees == [{"isolation_level": "AUTOCOMMIT"}]
    sql = [(requete, isolation) for requete, _, isolation in postgres_factice.requetes]
    assert ("VACUUM (ANALYZE)", "AUTOCOMMIT") in sql
    ordre = [requete for requete, _ in sql]
    assert ordre.index("VACUUM (ANALYZE)") < ordre.index(
        "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), :d)")
    assert [p for s, p in requetes if s.startswith("SELECT pg_relation_size")] == [
        {"t": "abonnements"}, {"t": "paiements"}, {"t": "pays"}]
    assert [s for s, _ in requetes if s.startswith("SELECT count")] == [
        "SELECT count(*) FROM abonnements", "SELECT count(*) FROM paiements",
        "SELECT count(*) FROM pays"]


def test_sans_reglage_les_mesures_postgres_chargent_soixante_jours_de_huit_cents_paiements(
        postgres_factice):
    """Réglages par défaut observés en appelant sans les donner (pas en lisant la signature)."""
    resultat = mesures.mesurer_postgres("postgresql+psycopg://u@h:5432/b")

    assert postgres_factice.referentiels == [(postgres_factice, 62)]
    assert [lot[2] for lot in postgres_factice.lots] == 60 * [800]
    assert [lot[3] for lot in postgres_factice.lots] == list(range(1000, 1060))
    assert resultat["jours"] == 60
    assert resultat["paiements"] == 60 * 800


def test_la_taille_par_ligne_est_arrondie_a_une_decimale_et_une_requete_inattendue_echoue(
        postgres_factice):
    postgres_factice.tailles_par_table["pays"] = (2000, 253952, 65536, 352256)

    tables = mesures.mesurer_postgres("u", jours=1, par_jour=1)["tables"]

    assert tables["pays"]["octets_par_ligne"] == 176.1  # 352 256 / 2 000 = 176,128

    del postgres_factice.reponses["SHOW server_version"]
    with pytest.raises(KeyError):  # le faux moteur ne répond qu'aux requêtes attendues
        mesures.mesurer_postgres("u", jours=1, par_jour=1)


# ---------------------------------------------------------------- qualité des alertes


class ModeleParMontant:
    """Faux modèle : la probabilité de fraude est le montant divisé par 1 000 (plafonné à 1)."""

    def predict_proba(self, variables):
        p = np.minimum(variables["montant_usd"].to_numpy(dtype=float) / 1000.0, 1.0)
        return np.column_stack([1.0 - p, p])


def _flux(montants, fraudes):
    return pd.DataFrame({
        "montant_usd": montants, "heure": 12, "pays_different": 0, "nuit": 0,
        "anciennete_jours": 100, "mobile": 0, "fraude": fraudes})


def test_les_alertes_comptent_precision_et_rappel_sans_diviser_par_zero():
    donnees = _flux([900, 850, 800, 100, 790, 50], [1, 0, 1, 1, 0, 0])

    resultat = mesures.alertes_sur_le_flux(ModeleParMontant(), donnees)

    # Alertes au score >= 0,8 : 900, 850 et 800 (le seuil est inclus). Parmi elles, 2 fraudes
    # (900 et 800) sur 3 fraudes au total (900, 800, 100).
    assert resultat == {"seuil": 0.8, "paiements": 6, "alertes": 3, "fraudes": 3,
                        "vrais_positifs": 2, "precision": 0.667, "rappel": 0.667}
    assert inspect.signature(mesures.alertes_sur_le_flux).parameters["seuil"].default == (
        pipeline.SEUIL_ALERTE)

    # Un seuil plus haut donne moins d'alertes.
    haut = mesures.alertes_sur_le_flux(ModeleParMontant(), _flux([900, 850, 800, 100],
                                                                 [1, 0, 1, 1]), seuil=0.85)
    assert (haut["seuil"], haut["alertes"], haut["vrais_positifs"]) == (0.85, 2, 1)
    assert haut["precision"] == 0.5 and haut["rappel"] == 0.333

    # Sans alerte la précision vaut zéro, sans fraude le rappel vaut zéro : jamais de division
    # par zéro.
    sans_alerte = mesures.alertes_sur_le_flux(ModeleParMontant(), _flux([10, 20, 30], [1, 0, 0]))
    assert (sans_alerte["alertes"], sans_alerte["precision"], sans_alerte["rappel"]) == (
        0, 0.0, 0.0)
    sans_fraude = mesures.alertes_sur_le_flux(ModeleParMontant(), _flux([900, 950], [0, 0]))
    assert (sans_fraude["alertes"], sans_fraude["fraudes"]) == (2, 0)
    assert (sans_fraude["precision"], sans_fraude["rappel"]) == (0.0, 0.0)

    # Les variables du modèle sont lues dans les données du flux, dans l'ordre.
    class ExigeLesVariables:
        def predict_proba(self, variables):
            assert list(variables.columns) == fraude.VARIABLES
            return np.array([[0.0, 1.0]])

    unique = mesures.alertes_sur_le_flux(ExigeLesVariables(), _flux([1], [1]))
    # Une seule alerte, et c'est une fraude : précision et rappel valent exactement 1.
    assert (unique["alertes"], unique["vrais_positifs"]) == (1, 1)
    assert (unique["precision"], unique["rappel"]) == (1.0, 1.0)

    # Des données sans étiquette ne se jugent pas.
    with pytest.raises(KeyError):
        mesures.alertes_sur_le_flux(ModeleParMontant(), _flux([900], [1]).drop(columns="fraude"))


# ---------------------------------------------------------------- mesures complémentaires


class ModeleFlux:
    def predict_proba(self, variables):
        return ModeleParMontant().predict_proba(variables)


def entrainer_factice(cx):
    _modele, mesures_modele, scores = faux_entrainer(cx)
    return ModeleFlux(), mesures_modele, scores


@pytest.fixture(scope="module")
def sorties_de_reference(tmp_path_factory):
    """Les fichiers d'une exécution en petit : 360 faits en batch, puis 1 000 dans le flux."""
    dossier = tmp_path_factory.mktemp("mesures") / "sorties"
    with pytest.MonkeyPatch.context() as mp:
        executer_en_petit(mp, dossier, jours=6, par_jour=60, avec_documents=False)
    return dossier


@pytest.fixture(scope="module")
def complements(sorties_de_reference):
    """`mesures_complementaires(360)`, calculé une fois (horloge et modèle faux).

    Rend le résultat, les connexions DuckDB ouvertes (arguments, options et journal de leurs
    instructions) et les moteurs SQLAlchemy créés (URL et instructions) : DuckDB et SQLite
    ignorent la casse du SQL, seul le texte envoyé prouve ce qui est écrit.
    """
    lectures = {"n": 0}

    def horloge():
        lectures["n"] += 1
        return lectures["n"] * PAS_D_HORLOGE

    connexions, moteurs = [], []
    vraie_connexion, vrai_moteur = duckdb.connect, mesures.create_engine

    def connexion_espionne(*arguments, **options):
        connexions.append((arguments, options, ConnexionEspion(
            vraie_connexion(*arguments, **options))))
        return connexions[-1][2]

    def moteur_espionne(url, *arguments, **options):
        engine = vrai_moteur(url, *arguments, **options)
        moteurs.append((url, noter_les_requetes_sqlalchemy(engine)))
        return engine

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mesures.pipeline, "SORTIES", sorties_de_reference)
        mp.setattr(mesures.fraude, "entrainer", entrainer_factice)
        mp.setattr(mesures.time, "perf_counter", horloge)
        mp.setattr(mesures.duckdb, "connect", connexion_espionne)
        mp.setattr(mesures, "create_engine", moteur_espionne)
        resultat = mesures.mesures_complementaires(360)
    return SimpleNamespace(resultat=resultat, connexions=connexions, moteurs=moteurs)


def test_les_mesures_complementaires_et_un_entrepot_absent_refuse(complements,
                                                                   sorties_de_reference,
                                                                   tmp_path, monkeypatch):
    mesures_c = complements.resultat
    assert list(mesures_c) == [
        "parquet_faits_octets", "parquet_octets_par_fait", "entrainement_modele_s",
        "mesures_modele", "alertes_flux", "prevalence_fraude_periode_test",
        "json_paiement_octets_moyens"]

    # La taille du fichier Parquet est celle du fichier écrit, qui contient tous les faits.
    parquet = sorties_de_reference / "faits_paiements.parquet"
    assert mesures_c["parquet_faits_octets"] == parquet.stat().st_size > 0
    assert mesures_c["parquet_octets_par_fait"] == round(parquet.stat().st_size / 1360, 1)
    lus = duckdb.connect().execute(
        f"SELECT count(*) FROM read_parquet('{parquet.as_posix()}')").fetchall()[0][0]
    assert lus == 1360

    # Durée d'entraînement arrondie à une décimale ; le modèle est entraîné sur le batch seul.
    assert mesures_c["entrainement_modele_s"] == 0.1
    assert mesures_c["mesures_modele"] == {
        "taux_fraude": 0.025, "pr_auc_test": 0.5, "roc_auc_test": 0.9,
        "lignes_apprentissage": 360, "lignes_test": 0}

    # Les alertes sont évaluées sur les 1 000 paiements du flux.
    entrepot = duckdb.connect(str(sorties_de_reference / "olap.duckdb"), read_only=True)
    flux = entrepot.execute(
        "SELECT f.montant_usd, f.est_conteste::INTEGER AS fraude FROM faits_paiements f "
        "WHERE f.id_paiement > 360").df()
    alerte = np.minimum(flux["montant_usd"].to_numpy() / 1000.0, 1.0) >= 0.8
    alertes = mesures_c["alertes_flux"]
    assert alertes["paiements"] == 1000
    assert alertes["alertes"] == int(alerte.sum())
    assert alertes["fraudes"] == int(flux["fraude"].sum())
    assert alertes["vrais_positifs"] == int((alerte & (flux["fraude"].to_numpy() == 1)).sum())
    assert alertes["seuil"] == pipeline.SEUIL_ALERTE

    # La prévalence est celle de la période de test du modèle.
    batch = entrepot.execute(
        "SELECT d.date AS date, f.est_conteste::INTEGER AS fraude FROM faits_paiements f "
        "JOIN dim_date d USING (cle_date) WHERE f.id_paiement <= 360 "
        "ORDER BY d.date, f.id_paiement").df()
    test = batch[batch["date"] > batch["date"].quantile(0.8)]
    assert mesures_c["prevalence_fraude_periode_test"] == round(float(test["fraude"].mean()), 4)

    # La taille moyenne d'un message JSON vient des 1 000 premiers paiements.
    limite = 1000
    connexion = sqlite3.connect(sorties_de_reference / "oltp.sqlite")
    connexion.row_factory = sqlite3.Row
    lignes = connexion.execute(f"SELECT * FROM paiements LIMIT {limite}").fetchall()
    connexion.close()
    tailles = [len(json.dumps(dict(ligne), default=str).encode()) for ligne in lignes]
    assert len(lignes) == limite  # 360 paiements du batch + 1 000 du flux : la limite s'applique
    assert mesures_c["json_paiement_octets_moyens"] == round(sum(tailles) / len(tailles), 1)

    # Les bases ouvertes et le texte exact des instructions (DuckDB et SQLite ignorent la casse).
    (arguments, options, source), (args_memoire, _, memoire), (args_flux, _, flux) = (
        complements.connexions)
    assert (arguments, options) == ((str(sorties_de_reference / "olap.duckdb"),),
                                    {"read_only": True})
    assert args_memoire == args_flux == (":memory:",)
    copie = f"COPY (SELECT * FROM faits_paiements) TO '{parquet.as_posix()}' (FORMAT PARQUET)"
    assert source.textes() == [
        copie, "SELECT count(*) FROM faits_paiements",
        "SELECT * FROM faits_paiements WHERE id_paiement <= 360",
        "SELECT * FROM dim_client", "SELECT * FROM dim_date",
        "SELECT * FROM faits_paiements WHERE id_paiement > 360",
        "SELECT * FROM dim_client", "SELECT * FROM dim_date", "close()"]
    requete = " ".join(fraude.REQUETE_ENTRAINEMENT.split())
    tables = ["register(faits_paiements)", "register(dim_client)", "register(dim_date)"]
    assert memoire.textes() == [
        *tables, "SELECT count(*) FROM faits_paiements", requete, "close()"]
    assert flux.textes() == [*tables, requete, "close()"]
    ((url, sql_sqlite),) = complements.moteurs
    assert url == f"sqlite:///{sorties_de_reference / 'oltp.sqlite'}"
    assert [r for r in sql_sqlite if "paiements" in r] == ["SELECT * FROM paiements LIMIT 1000"]

    # Sans fichier d'entrepôt, la mesure est refusée.
    monkeypatch.setattr(mesures.pipeline, "SORTIES", tmp_path)
    with pytest.raises(duckdb.Error):
        mesures.mesures_complementaires(360)


def test_les_arrondis_des_mesures_complementaires_sur_un_entrepot_fait_a_la_main(
        tmp_path, monkeypatch):
    """Chaque valeur arrondie est contrôlée sur des quotients qui ont beaucoup de chiffres.

    Sur la démonstration en petit, la prévalence vaut une fraction à peu de décimales et la taille
    du fichier Parquet dépend de la bibliothèque : l'arrondi (4 chiffres, 1 chiffre) ne s'y voit
    pas. Ici l'entrepôt est écrit à la main : 16 faits de batch (un par jour, donc les trois
    derniers jours forment la période de test, dont un seul fait contesté : 1/3), puis 2 faits du
    flux. La taille du fichier Parquet est fixée à 1 000 001 octets pour 18 faits.
    """
    sorties = tmp_path / "sorties"
    sorties.mkdir()
    entrepot = olap.connecter(str(sorties / "olap.duckdb"))
    entrepot.execute("INSERT INTO dim_client VALUES (1, 'FR', 100, 'occasionnel')")
    for rang in range(18):
        jour = date(2026, 8, 1) + timedelta(days=rang)
        cle_date = int(jour.strftime("%Y%m%d"))
        entrepot.execute("INSERT INTO dim_date VALUES (?, ?, ?, ?, ?, ?, ?)", [
            cle_date, jour, jour.year, jour.month, jour.day, jour.isoweekday(),
            jour.isoweekday() >= 6])
        conteste = rang == 14  # le 15e jour : un seul fait contesté dans les trois derniers
        entrepot.execute(
            "INSERT INTO faits_paiements VALUES (?, ?, 1, 1, 1, 'FR', 'EUR', 'mobile', 12, 50.0, "
            "TRUE, FALSE, ?, NULL)", [rang + 1, cle_date, conteste])
    entrepot.close()
    base_oltp = sqlite3.connect(sorties / "oltp.sqlite")
    base_oltp.execute("CREATE TABLE paiements (id_paiement INTEGER, statut TEXT)")
    base_oltp.execute("INSERT INTO paiements VALUES (1, 'reussi')")
    base_oltp.commit()
    base_oltp.close()

    vrai_stat = Path.stat

    def taille_fixee(self, *arguments, **options):
        if self.name == "faits_paiements.parquet":
            return SimpleNamespace(st_size=1_000_001)
        return vrai_stat(self, *arguments, **options)

    monkeypatch.setattr(Path, "stat", taille_fixee)
    monkeypatch.setattr(mesures.pipeline, "SORTIES", sorties)
    monkeypatch.setattr(mesures.fraude, "entrainer", entrainer_factice)

    resultat = mesures.mesures_complementaires(16)

    assert resultat["parquet_faits_octets"] == 1_000_001
    assert resultat["parquet_octets_par_fait"] == 55555.6  # 1 000 001 / 18 = 55 555,611…
    assert resultat["prevalence_fraude_periode_test"] == 0.3333  # 1 / 3 = 0,33333…
    assert resultat["json_paiement_octets_moyens"] == len(
        json.dumps({"id_paiement": 1, "statut": "reussi"}).encode())
    assert resultat["alertes_flux"]["paiements"] == 2


# ---------------------------------------------------------------- mesures de la démonstration


class FauxProcessus:
    """Faux psutil.Process : chaque lecture de la mémoire rend une valeur plus grande."""

    lectures = 0

    def memory_info(self):
        FauxProcessus.lectures += 1
        return type("Memoire", (), {"rss": 1000 * FauxProcessus.lectures})()


@pytest.fixture
def demonstration_factice(monkeypatch, tmp_path):
    """Remplace l'exécution, les mesures complémentaires, l'horloge et le processus mesuré."""
    import psutil

    FauxProcessus.lectures = 0
    (tmp_path / "oltp.sqlite").write_bytes(b"o" * 1234)
    (tmp_path / "olap.duckdb").write_bytes(b"d" * 567)
    monkeypatch.setattr(mesures.pipeline, "SORTIES", tmp_path)
    monkeypatch.setattr(psutil, "Process", FauxProcessus)
    rapport = {"oltp": {"paiements": 48000}, "batch": {"faits": 48000},
               "flux": {"latence_mediane_ms": 9.1}}
    appels = {}

    def executer_factice():
        # Attend que l'échantillonneur ait lu la mémoire plusieurs fois, au plus 5 secondes.
        attente = threading.Event()
        for _ in range(500):
            if FauxProcessus.lectures >= 4:
                break
            attente.wait(0.01)
        appels["executer"] = True
        return rapport

    def complements(faits_batch):
        appels["faits_batch"] = faits_batch
        return {"parquet_faits_octets": 42}

    monkeypatch.setattr(mesures.pipeline, "executer", executer_factice)
    monkeypatch.setattr(mesures, "mesures_complementaires", complements)
    lectures = {"n": 0}

    def horloge():
        lectures["n"] += 1
        return lectures["n"] * PAS_D_HORLOGE

    monkeypatch.setattr(mesures.time, "perf_counter", horloge)
    return appels


def test_la_demonstration_est_mesuree_en_duree_memoire_et_fichiers_et_un_fichier_manquant_refuse(
        demonstration_factice, monkeypatch, tmp_path):
    fils = []
    vrai_fil = threading.Thread

    def fil_note(*args, **kwargs):
        fils.append(kwargs)
        return vrai_fil(*args, **kwargs)

    sommeils = []
    vrai_sommeil = mesures.time.sleep
    monkeypatch.setattr(mesures.threading, "Thread", fil_note)
    monkeypatch.setattr(mesures.time, "sleep", lambda s: (sommeils.append(s), vrai_sommeil(0.001)))

    resultat = mesures.mesurer_demo()

    assert list(resultat) == ["duree_s", "pic_rss_octets", "fichiers_octets", "paiements",
                              "faits", "latence_mediane_ms", "parquet_faits_octets"]
    assert resultat["duree_s"] == 0.1
    assert resultat["fichiers_octets"] == {"oltp.sqlite": 1234, "olap.duckdb": 567}
    assert (resultat["paiements"], resultat["faits"], resultat["latence_mediane_ms"]) == (
        48000, 48000, 9.1)
    assert resultat["parquet_faits_octets"] == 42
    assert demonstration_factice == {"executer": True, "faits_batch": 48000}
    # Les lectures croissent (1 000, 2 000, ...) : le pic est la dernière, jamais la première.
    assert resultat["pic_rss_octets"] == 1000 * FauxProcessus.lectures
    assert resultat["pic_rss_octets"] >= 4000
    # L'échantillonneur est un fil daemon qui dort 20 ms entre deux lectures.
    assert [(f["daemon"], callable(f["target"])) for f in fils] == [(True, True)]
    assert sommeils and set(sommeils) == {0.02}

    (tmp_path / "olap.duckdb").unlink()
    with pytest.raises(FileNotFoundError):
        mesures.mesurer_demo()


# ---------------------------------------------------------------- commande


@pytest.fixture
def commande(monkeypatch, tmp_path):
    appels = []
    monkeypatch.setattr(mesures, "SORTIES", tmp_path / "sorties")
    monkeypatch.setattr(mesures, "mesurer_postgres", lambda url, jours, par_jour: (
        appels.append(("postgres", url, jours, par_jour)) or {"serveur": "16", "note": "é"}))
    monkeypatch.setattr(mesures, "mesurer_demo", lambda: (
        appels.append(("demo",)) or {"duree_s": 1.5, "note": "démo"}))
    return appels


def test_la_commande_transmet_ses_options_ecrit_un_json_utf8_par_commande_et_refuse_le_reste(
        commande, tmp_path, capsys, lecture_ecriture_en_utf8, monkeypatch):
    appels_json = noter_les_appels_json(monkeypatch)
    mesures.main(["postgres", "--url", "postgresql+psycopg://u@h/b"])
    mesures.main(["postgres", "--url", "u", "--jours", "7", "--par-jour", "50"])
    mesures.main(["demo"])  # le dossier existe déjà : pas d'erreur

    # Chaque commande écrit le JSON dans le fichier puis l'affiche : accents conservés, 2 espaces.
    assert appels_json == 6 * [{"ensure_ascii": False, "indent": 2}]

    assert commande == [("postgres", "postgresql+psycopg://u@h/b", 60, 800),
                        ("postgres", "u", 7, 50), ("demo",)]
    attendu = json.dumps({"duree_s": 1.5, "note": "démo"}, ensure_ascii=False, indent=2)
    ecrit = (tmp_path / "sorties" / "mesures_demo.json").read_bytes().decode("utf-8")
    assert ecrit.replace("\r\n", "\n") == attendu
    assert attendu + "\n" in capsys.readouterr().out
    assert sorted(p.name for p in (tmp_path / "sorties").iterdir()) == [
        "mesures_demo.json", "mesures_postgres.json"]
    assert "é" in (tmp_path / "sorties" / "mesures_postgres.json").read_text(encoding="utf-8")
    assert all(o["encodage"] for o in lecture_ecriture_en_utf8)

    # URL absente, commande inconnue ou absente, option invalide : code 2, rien n'est mesuré.
    commande.clear()
    for arguments in (["postgres"], ["supprimer"], [], ["postgres", "--url", "u", "--jours", "x"],
                      ["postgres", "--url", "u", "--par-jour", "1.5"]):
        with pytest.raises(SystemExit) as sortie:
            mesures.main(arguments)
        assert sortie.value.code == 2, arguments
    assert commande == []
    assert "--url" in capsys.readouterr().err


def test_l_aide_decrit_chaque_commande_et_chaque_option(commande, capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")

    with pytest.raises(SystemExit):
        mesures.main(["--help"])
    general = capsys.readouterr().out
    with pytest.raises(SystemExit):
        mesures.main(["postgres", "--help"])
    postgres = capsys.readouterr().out

    assert mesures.__doc__.splitlines()[0] in general
    assert re.search(r"postgres\s+mesurer l'occupation disque dans PostgreSQL$", general, re.M)
    assert re.search(r"demo\s+mesurer la démonstration autonome$", general, re.M)
    assert re.search(r"--url URL\s+URL SQLAlchemy \(pilote psycopg\)$", postgres, re.M)


def test_les_mesures_vont_dans_le_dossier_sorties_et_la_commande_se_lance_par_python_m():
    """Le dossier d'écriture est `sorties/` à la racine du projet, et le point d'entrée existe."""
    racine = Path(__file__).resolve().parent.parent
    assert mesures.SORTIES == racine / "sorties"

    # Lancée en tant que programme, la commande affiche son aide (sans point d'entrée, le processus
    # se terminerait en silence avec le code 0).
    resultat = subprocess.run(
        [sys.executable, "-m", "stripe_demo.mesures", "--help"], cwd=racine, capture_output=True,
        check=False, timeout=120, env={**os.environ, "PYTHONPATH": str(racine)})
    assert resultat.returncode == 0
    assert b"postgres" in resultat.stdout and b"demo" in resultat.stdout

    # Une commande absente est refusée (code 2) : le point d'entrée est bien celui de `main`.
    sans_commande = subprocess.run(
        [sys.executable, "-m", "stripe_demo.mesures"], cwd=racine, capture_output=True,
        check=False, timeout=120, env={**os.environ, "PYTHONPATH": str(racine)})
    assert sans_commande.returncode == 2
