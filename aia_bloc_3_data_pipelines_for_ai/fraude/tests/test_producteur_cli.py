# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Producteur : boucle API, rejeu du jeu et ligne de commande (options, codes de sortie).

Les tests de ``main`` isolent l'environnement comme la fixture ``config`` (dossier de données,
sel de hachage et jeu dans ``tmp_path``) et remplacent ce qui toucherait le monde extérieur :
``requests.get``, l'installation des gestionnaires de signaux et la configuration du journal.
Aucun test n'ouvre de connexion réseau ni n'attend réellement.
"""

from __future__ import annotations

import json
import re
import signal as signal_reel
import sqlite3
import sys
import threading
import types
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pytest
import requests

import detection_fraude.producteur as producteur_module
from detection_fraude.configuration import ErreurConfiguration, Relances
from detection_fraude.file_attente import FileSQLite
from detection_fraude.jeu_donnees import COLONNES_API
from detection_fraude.masquage import HacheurCarte
from detection_fraude.producteur import (
    BilanProduction,
    PannesSimulees,
    Producteur,
    boucle_api,
    ecrire_etat,
    main,
    rejouer_jeu,
)
from tests.helpers import ligne_brute

SEL = "sel-de-test-uniquement-0123456789abcdef"
URL_API = "https://api.test/current"
HORODATAGE_FIXE = "2026-10-10T08:30:15.123456+00:00"
CONFIG_DEV = """\
environnement: dev
masquage:
  longueur_hash: 24
source:
  type: api
  api_url: "https://api.test/current"
  intervalle_s: 0
  delai_attente_s: 4.5
  relances:
    max_tentatives: 3
    attente_initiale_s: 0.0
    facteur: 1.0
    attente_max_s: 0.0
    gigue: 0.0
file:
  type: sqlite
  chemin_sqlite: "{donnees}/file.db"
journal:
  niveau: warning
  console: json
  fichier: "{donnees}/journaux/audit.jsonl"
"""
CONFIG_PROD = """\
environnement: prod
source:
  type: api
  api_url: "https://api.test/current"
file:
  type: kafka
  kafka_serveurs: "courtier:9092"
journal:
  niveau: info
  console: json
  fichier: null
"""
DATES_JEU = [f"2026-10-09 10:00:{n:02d}" for n in range(8)] + [
    f"2026-10-10 10:00:{n:02d}" for n in range(4)
]


# --------------------------------------------------------------------------------------------
# Doublures
# --------------------------------------------------------------------------------------------
class Reponse:
    def __init__(self, statut: int = 200, corps: str | bytes = b"") -> None:
        self.status_code = statut
        self.content = corps if isinstance(corps, bytes) else corps.encode()
        self.headers: dict[str, str] = {}


def reponse_api(lignes: list[dict[str, Any]]) -> Reponse:
    colonnes = list(lignes[0])
    document = {
        "columns": colonnes,
        "index": list(range(len(lignes))),
        "data": [[ligne[c] for c in colonnes] for ligne in lignes],
    }
    return Reponse(200, json.dumps(json.dumps(document)))


class GetScripte:
    """Renvoie (ou lève) les éléments d'un scénario et garde chaque appel reçu."""

    def __init__(self, *scenario: Any) -> None:
        self.scenario = list(scenario)
        self.appels: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.appels.append((args, kwargs))
        if not self.scenario:
            raise AssertionError("appel HTTP imprévu : le scénario est épuisé")
        element = self.scenario.pop(0)
        if isinstance(element, Exception):
            raise element
        return element


class FileEspion:
    """File en mémoire : garde ce qui est publié, compte les accusés de réception."""

    def __init__(self) -> None:
        self.publies: list[tuple[str, str | None, dict[str, Any]]] = []
        self.vidages = 0
        self.fermetures = 0

    def publier(self, sujet: str, valeur: dict[str, Any], cle: str | None = None) -> None:
        self.publies.append((sujet, cle, valeur))

    def vider(self) -> None:
        self.vidages += 1

    def fermer(self) -> None:
        self.fermetures += 1


class ProducteurLots:
    """Producteur factice : garde chaque lot reçu et le déclare entièrement publié."""

    def __init__(self) -> None:
        self.lots: list[list[Any]] = []

    def traiter_lignes(self, lignes: Any) -> BilanProduction:
        lot = list(lignes)
        self.lots.append(lot)
        return BilanProduction(lues=len(lot), publiees=len(lot))


class HorlogeFausse:
    """Remplace le module ``time`` du producteur : les pauses sont notées, jamais attendues."""

    def __init__(self) -> None:
        self.pauses: list[float] = []

    def monotonic(self) -> float:
        return 0.0

    def sleep(self, secondes: float) -> None:
        self.pauses.append(secondes)


class EvenementSansAttente(threading.Event):
    """Événement d'arrêt dont ``wait`` note son argument au lieu de bloquer."""

    def __init__(self) -> None:
        super().__init__()
        self.attentes: list[Any] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.attentes.append(timeout)
        return self.is_set()


class FileSuivie(FileSQLite):
    """Vraie file SQLite qui compte les fermetures."""

    instances: list[FileSuivie] = []

    def __init__(self, chemin: Any) -> None:
        super().__init__(chemin)
        self.fermetures = 0
        FileSuivie.instances.append(self)

    def fermer(self) -> None:
        self.fermetures += 1
        super().fermer()


@contextmanager
def chien_de_garde(arret: threading.Event, delai: float = 1.0):
    """Lève l'arrêt après ``delai`` s si la boucle ne s'est pas arrêtée d'elle-même."""
    minuterie = threading.Timer(delai, arret.set)
    minuterie.daemon = True
    minuterie.start()
    try:
        yield
    finally:
        minuterie.cancel()
    assert not arret.is_set(), "la boucle ne s'est pas arrêtée d'elle-même"


def figer_horloge(monkeypatch: pytest.MonkeyPatch) -> None:
    class DatetimeFixe:
        @staticmethod
        def now(tz: Any = None) -> datetime:
            return datetime(2026, 10, 10, 8, 30, 15, 123456, tzinfo=tz)

    monkeypatch.setattr(producteur_module, "datetime", DatetimeFixe)


def ecrire_jeu(chemin, dates: list[str]):
    """Écrit un petit jeu au format de ``fraudTest.csv`` : une ligne valide par date."""
    enregistrements = []
    for rang, date_heure in enumerate(dates):
        ligne = ligne_brute(rang)
        enregistrement: dict[str, Any] = {"trans_date_trans_time": date_heure}
        enregistrement.update({colonne: ligne[colonne] for colonne in COLONNES_API})
        enregistrements.append(enregistrement)
    pd.DataFrame(enregistrements).to_csv(chemin, index=False)
    return chemin


def dates_consecutives(nombre: int) -> list[str]:
    debut = datetime(2026, 10, 9, tzinfo=UTC)
    return [(debut + timedelta(seconds=n)).strftime("%Y-%m-%d %H:%M:%S") for n in range(nombre)]


def lire_etat(config):
    chemin = config.racine_donnees / "etat" / "producteur.json"
    return json.loads(chemin.read_text(encoding="utf-8"))


def une_tentative(config):
    return replace(config, source=replace(config.source, relances=Relances(max_tentatives=1)))


def messages_en_base(chemin):
    """Messages publiés dans la base SQLite de la file, lus sans passer par ``FileSQLite``."""
    base = sqlite3.connect(chemin)
    try:
        lignes = base.execute("SELECT sujet, cle, valeur FROM messages ORDER BY id").fetchall()
    finally:
        base.close()
    return [(sujet, cle, json.loads(valeur)) for sujet, cle, valeur in lignes]


@pytest.fixture
def file_espion():
    return FileEspion()


@pytest.fixture
def producteur(config, file_espion, hacheur):
    return Producteur(config, file_espion, hacheur, source="api")


# --------------------------------------------------------------------------------------------
# ecrire_etat
# --------------------------------------------------------------------------------------------
def test_ecrire_etat_remplace_le_contenu_et_un_etat_non_serialisable_laisse_le_fichier_inchange(
    config,
):
    """L'état de santé est écrit en UTF-8 lisible ; un état invalide ne détruit pas l'ancien."""
    chemin = config.racine_donnees / "etat" / "producteur.json"
    assert not chemin.parent.exists()
    ecrire_etat(config, {"message": "été", "n": 1})
    assert chemin.read_bytes() == '{"message": "été", "n": 1}'.encode()
    ecrire_etat(config, {"message": "remplacé"})
    contenu_avant = chemin.read_bytes()
    assert contenu_avant == '{"message": "remplacé"}'.encode()
    with pytest.raises(TypeError) as exc:
        ecrire_etat(config, {"invalide": object()})
    assert str(exc.value) == "Object of type object is not JSON serializable"
    assert chemin.read_bytes() == contenu_avant


# --------------------------------------------------------------------------------------------
# boucle_api : succès, échecs alternés, pauses
# --------------------------------------------------------------------------------------------
def test_un_cycle_abandonne_apres_un_succes_laisse_le_bilan_cumule_inchange(
    config, file_espion, producteur, evenements, monkeypatch
):
    """Succès, panne, succès, pannes : le cumul ne bouge pas à l'échec, le compteur repart à 0."""
    figer_horloge(monkeypatch)
    config_courte = une_tentative(config)
    cycle_un = (
        [ligne_brute(1)] * 3 + [ligne_brute(2, amt="abc")] + [ligne_brute(n) for n in (3, 4, 5)]
    )
    get = GetScripte(
        reponse_api(cycle_un),
        requests.ConnectionError("x"),
        reponse_api([ligne_brute(1), ligne_brute(6)]),
        requests.Timeout("y"),
        requests.ConnectionError("z"),
    )
    etats_vus: list[dict[str, Any]] = []
    pauses: list[float] = []

    def dormir(secondes):
        pauses.append(secondes)
        etats_vus.append(lire_etat(config))

    arret = threading.Event()
    with chien_de_garde(arret):
        bilan = boucle_api(
            config_courte,
            producteur,
            arret,
            nombre_appels=5,
            intervalle_s=3,
            get=get,
            dormir=dormir,
        )
    assert bilan == BilanProduction(lues=9, publiees=5, rejetees=1, doublons=3)
    assert pauses == [3, 3, 3, 3]
    cumul_un = {"lues": 7, "publiees": 4, "rejetees": 1, "doublons": 2}
    cumul_deux = {"lues": 9, "publiees": 5, "rejetees": 1, "doublons": 3}
    reussite = "derniere_reussite"
    tentative = "derniere_tentative"
    assert etats_vus == [
        {reussite: HORODATAGE_FIXE, "echecs_consecutifs": 0, **cumul_un},
        {tentative: HORODATAGE_FIXE, "echecs_consecutifs": 1, **cumul_un},
        {reussite: HORODATAGE_FIXE, "echecs_consecutifs": 0, **cumul_deux},
        {tentative: HORODATAGE_FIXE, "echecs_consecutifs": 1, **cumul_deux},
    ]
    publiees_avant = etats_vus[0]["publiees"]
    assert etats_vus[1]["publiees"] == publiees_avant
    assert lire_etat(config) == {tentative: HORODATAGE_FIXE, "echecs_consecutifs": 2, **cumul_deux}
    assert evenements.nommes("cycle_termine") == [
        ("INFO", {"lues": 7, "publiees": 4, "rejetees": 1, "doublons": 2}),
        ("INFO", {"lues": 2, "publiees": 1, "rejetees": 0, "doublons": 1}),
    ]
    assert evenements.nommes("cycle_abandonne") == [
        ("ERROR", {"cause": "1 tentatives échouées (ConnectionError)", "echecs_consecutifs": 1}),
        ("ERROR", {"cause": "1 tentatives échouées (Timeout)", "echecs_consecutifs": 1}),
        ("ERROR", {"cause": "1 tentatives échouées (ConnectionError)", "echecs_consecutifs": 2}),
    ]
    assert (
        get.appels == [((config.source.api_url,), {"timeout": config.source.delai_attente_s})] * 5
    )


@pytest.mark.parametrize(
    ("injecter", "panne_initiale", "nombre", "intervalle", "attendues"),
    [
        (True, False, 3, None, [60.0, 60.0]),
        (True, False, 3, 0, [0, 0]),
        (True, False, 2, 2.5, [2.5]),
        (True, False, 1, 9, []),
        (True, False, 4, 0.5, [0.5, 0.5, 0.5]),
        (False, False, 3, None, [60.0, 60.0]),
        (False, False, 2, 7, [7]),
        (False, False, 1, 7, []),
        (True, True, 2, 7, [0.5, 7]),
        (False, True, 2, 7, [0.5, 7]),
    ],
)
def test_la_pause_vient_de_la_configuration_sauf_argument_et_manque_apres_le_dernier(
    config, producteur, monkeypatch, injecter, panne_initiale, nombre, intervalle, attendues
):
    """Pause = argument (même 0) sinon configuration ; doublure injectée ou ``arreter.wait``."""
    relances = Relances(
        max_tentatives=3, attente_initiale_s=0.5, facteur=2.0, attente_max_s=5.0, gigue=0.0
    )
    config_pauses = replace(config, source=replace(config.source, relances=relances))
    scenario: list[Any] = [reponse_api([ligne_brute(n)]) for n in range(1, nombre + 1)]
    if panne_initiale:
        scenario.insert(0, requests.ConnectionError("x"))
    get = GetScripte(*scenario)
    pauses: list[Any] = []
    arret = threading.Event()
    options: dict[str, Any] = {}
    if injecter:
        options["dormir"] = pauses.append
    else:
        monkeypatch.setattr(arret, "wait", lambda secondes=None: pauses.append(secondes) or False)
    with chien_de_garde(arret):
        bilan = boucle_api(
            config_pauses,
            producteur,
            arret,
            nombre_appels=nombre,
            intervalle_s=intervalle,
            get=get,
            **options,
        )
    assert pauses == attendues
    assert bilan == BilanProduction(lues=nombre, publiees=nombre)
    assert [args for args, _ in get.appels] == [(config.source.api_url,)] * len(scenario)


@pytest.mark.parametrize(
    ("nombre", "cadence", "lots", "pas"),
    [
        (1, 0.0, [1], None),
        (499, 0.0, [499], None),
        (500, 0.0, [500], None),
        (501, 0.0, [500, 1], None),
        (1000, 0.0, [500, 500], None),
        (1201, 0.0, [500, 500, 201], None),
        (3, 4.0, [1, 1, 1], 0.25),
        (3, 0.5, [1, 1, 1], 2.0),
        (3, 1.0, [1, 1, 1], 1.0),
        (3, 1000.0, [1, 1, 1], 0.001),
        (3, -3.0, [3], None),
        (3, -0.5, [3], None),
    ],
)
def test_le_jeu_est_rejoue_par_lots_de_500_sans_cadence_et_ligne_a_ligne_avec_cadence(
    config, evenements, monkeypatch, nombre, cadence, lots, pas
):
    """Cadence c > 0 : un lot par ligne puis pause de 1/c s ; sinon lots de 500 lignes."""
    horloge = HorlogeFausse()
    monkeypatch.setattr(producteur_module, "time", horloge)
    ecrire_jeu(config.source.jeu_chemin, dates_consecutives(nombre))
    producteur = ProducteurLots()
    bilan = rejouer_jeu(
        config,
        producteur,
        jour=None,
        limite=None,
        corruptions=0,
        graine=7,
        cadence=cadence,
        arreter=threading.Event(),
    )
    assert [len(lot) for lot in producteur.lots] == lots
    rejouees = [ligne["trans_num"] for lot in producteur.lots for ligne in lot]
    assert rejouees == [ligne_brute(n)["trans_num"] for n in range(nombre)]
    assert horloge.pauses == ([pas] * nombre if pas else [])
    assert bilan == BilanProduction(lues=nombre, publiees=nombre)
    assert evenements.tous() == [
        (
            "INFO",
            "rejeu_termine",
            {"lues": nombre, "publiees": nombre, "rejetees": 0, "doublons": 0},
        )
    ]


def test_l_arret_demande_interrompt_le_rejeu_et_laisse_la_file_inchangee_ou_vide_le_lot_en_cours(
    config, file_espion, producteur, evenements, monkeypatch
):
    """Arrêt avant le début : rien n'est publié ; arrêt en cours : le lot commencé est traité."""
    monkeypatch.setattr(producteur_module, "time", HorlogeFausse())
    ecrire_jeu(config.source.jeu_chemin, dates_consecutives(5))
    arret = threading.Event()
    arret.set()
    publies_avant = list(file_espion.publies)
    bilan = rejouer_jeu(
        config,
        producteur,
        jour=None,
        limite=None,
        corruptions=0,
        graine=7,
        cadence=0.0,
        arreter=arret,
    )
    assert bilan == BilanProduction()
    assert file_espion.publies == publies_avant
    assert file_espion.vidages == 0

    arret_en_cours = threading.Event()
    produites: list[int] = []

    def lignes_puis_arret(chemin, **options):
        for rang in range(20):
            if rang == 7:
                arret_en_cours.set()
            produites.append(rang)
            yield ligne_brute(rang)

    monkeypatch.setattr(producteur_module, "lignes_du_jeu", lignes_puis_arret)
    lots = ProducteurLots()
    bilan = rejouer_jeu(
        config,
        lots,
        jour=None,
        limite=None,
        corruptions=0,
        graine=7,
        cadence=0.0,
        arreter=arret_en_cours,
    )
    assert produites == list(range(8))
    assert [[ligne["trans_num"] for ligne in lot] for lot in lots.lots] == [
        [ligne_brute(n)["trans_num"] for n in range(7)]
    ]
    assert bilan == BilanProduction(lues=7, publiees=7)
    assert evenements.nommes("rejeu_termine")[-1] == (
        "INFO",
        {"lues": 7, "publiees": 7, "rejetees": 0, "doublons": 0},
    )


def test_un_jeu_introuvable_arrete_le_rejeu_sans_rien_traiter(config, evenements):
    """Sans fichier, le rejeu lève l'erreur de lecture : aucun lot, aucun bilan journalisé."""
    producteur = ProducteurLots()
    with pytest.raises(FileNotFoundError) as exc:
        rejouer_jeu(
            config,
            producteur,
            jour=None,
            limite=None,
            corruptions=0,
            graine=7,
            cadence=0.0,
            arreter=threading.Event(),
        )
    assert str(exc.value).startswith(f"Jeu de données introuvable : {config.source.jeu_chemin}.")
    assert producteur.lots == []
    assert evenements.nommes("rejeu_termine") == []


# --------------------------------------------------------------------------------------------
# main : environnement isolé
# --------------------------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path, monkeypatch):
    """Environnement isolé : configuration, données et jeu dans ``tmp_path``, sans journal."""
    dossier = tmp_path / "config"
    dossier.mkdir()
    (dossier / "dev.yaml").write_text(CONFIG_DEV, encoding="utf-8")
    (dossier / "prod.yaml").write_text(CONFIG_PROD, encoding="utf-8")
    monkeypatch.setenv("FRAUDE_CONFIG_DIR", str(dossier))
    monkeypatch.setenv("FRAUDE_DONNEES", str(tmp_path / "donnees"))
    monkeypatch.setenv("FRAUDE_SEL_HACHAGE", SEL)
    monkeypatch.setenv("FRAUDE_JEU_CHEMIN", str(tmp_path / "jeu.csv"))
    for variable in (
        "FRAUDE_ENV",
        "FRAUDE_TYPE_FILE",
        "FRAUDE_TYPE_SOURCE",
        "FRAUDE_API_URL",
        "FRAUDE_KAFKA_SERVEURS",
        "FRAUDE_NIVEAU_JOURNAL",
        "FRAUDE_SEL_HACHAGE_FICHIER",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(sys, "argv", ["producteur"])
    monkeypatch.setenv("COLUMNS", "200")

    def reseau_interdit(*args: Any, **options: Any) -> Any:
        raise AssertionError("ce test ne doit faire aucun appel réseau")

    # Par défaut aucun appel HTTP : un défaut qui laisserait ``main`` démarrer la collecte échoue
    # tout de suite au lieu d'attendre un réseau. Les tests qui en ont besoin le remplacent.
    monkeypatch.setattr(requests, "get", reseau_interdit)
    monkeypatch.setattr(producteur_module, "time", HorlogeFausse())
    arrets: list[EvenementSansAttente] = []

    def fabriquer_evenement() -> EvenementSansAttente:
        arrets.append(EvenementSansAttente())
        return arrets[-1]

    monkeypatch.setattr(
        producteur_module, "threading", types.SimpleNamespace(Event=fabriquer_evenement)
    )
    appels_journal: list[tuple[Any, ...]] = []
    gestionnaires: list[tuple[int, Any]] = []
    monkeypatch.setattr(
        producteur_module, "configurer_journal", lambda *args: appels_journal.append(args)
    )
    faux_signal = types.SimpleNamespace(
        SIGINT=signal_reel.SIGINT,
        SIGTERM=signal_reel.SIGTERM,
        signal=lambda numero, gestionnaire: gestionnaires.append((numero, gestionnaire)),
    )
    monkeypatch.setattr(producteur_module, "signal", faux_signal)
    return types.SimpleNamespace(
        donnees=tmp_path / "donnees",
        jeu=tmp_path / "jeu.csv",
        base=tmp_path / "donnees" / "file.db",
        journal=appels_journal,
        signaux=gestionnaires,
        arrets=arrets,
    )


def test_l_aide_decrit_chaque_option_et_sort_avec_le_code_0_sans_rien_demarrer(env, capsys):
    """``--help`` affiche l'usage et quitte : ni journal, ni signaux, ni base créée."""
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    sortie = capsys.readouterr().out
    assert "Producteur : API ou rejeu du jeu -> validation -> masquage -> file." in sortie
    for fragment in (
        "[--source {api,jeu}]",
        "[--nombre NOMBRE]",
        "[--intervalle INTERVALLE]",
        "[--jour JOUR]",
        "[--cadence CADENCE]",
        "[--corruptions CORRUPTIONS]",
        "[--pannes PANNES]",
        "[--rebaser-sur REBASER_SUR]",
        "[--graine GRAINE]",
        "défaut : configuration",
        "API : nombre d'appels ; jeu : nombre de lignes",
        "API : secondes entre deux appels",
        "jeu : ne rejouer que ce jour (AAAA-MM-JJ)",
        "jeu : lignes par seconde",
        "jeu : lignes à casser (démo)",
        "API : pannes simulées (démo)",
        "jeu : décale les dates pour que --jour tombe à cette date (démo)",
    ):
        assert fragment in sortie
    assert env.journal == []
    assert env.signaux == []
    assert not env.donnees.exists()


def test_une_option_inconnue_est_refusee_avec_le_code_2_et_rien_n_est_demarre(env, capsys):
    """Une faute de frappe dans une option ne doit pas lancer le producteur avec les défauts."""
    with pytest.raises(SystemExit) as exc:
        main(["--source", "jeu", "--inconnue"])
    assert exc.value.code == 2
    erreur = capsys.readouterr().err
    assert (
        erreur.strip().splitlines()[-1] == "producteur: error: unrecognized arguments: --inconnue"
    )
    assert env.journal == []
    assert env.signaux == []
    assert not env.donnees.exists()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["--source", "ftp"],
            r"argument --source: invalid choice: 'ftp' \(choose from (?:'api', 'jeu'|api, jeu)\)",
        ),
        (["--nombre", "abc"], "argument --nombre: invalid int value: 'abc'"),
        (["--nombre", "1.5"], "argument --nombre: invalid int value: '1.5'"),
        (["--intervalle", "x"], "argument --intervalle: invalid float value: 'x'"),
        (["--cadence", "vite"], "argument --cadence: invalid float value: 'vite'"),
        (["--corruptions", "deux"], "argument --corruptions: invalid int value: 'deux'"),
        (["--pannes", "1.0"], "argument --pannes: invalid int value: '1.0'"),
        (["--graine", "x"], "argument --graine: invalid int value: 'x'"),
    ],
)
def test_une_valeur_d_option_invalide_est_refusee_avec_le_code_2(env, capsys, argv, message):
    """Chaque option typée refuse une valeur illisible avec un message d'argparse explicite."""
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    dernier = capsys.readouterr().err.strip().splitlines()[-1]
    motif = message if "choose from" in message else re.escape(message)
    assert re.fullmatch(f"producteur: error: {motif}", dernier)
    assert env.journal == []
    assert not env.donnees.exists()


@pytest.mark.parametrize(
    "option",
    [
        "--source",
        "--nombre",
        "--intervalle",
        "--jour",
        "--cadence",
        "--corruptions",
        "--pannes",
        "--rebaser-sur",
        "--graine",
    ],
)
def test_une_option_sans_valeur_est_refusee_avec_le_code_2(env, capsys, option):
    """Toutes les options prennent une valeur : en oublier une est une erreur, pas un défaut."""
    with pytest.raises(SystemExit) as exc:
        main([option])
    assert exc.value.code == 2
    dernier = capsys.readouterr().err.strip().splitlines()[-1]
    assert dernier == f"producteur: error: argument {option}: expected one argument"
    assert env.journal == []


def test_sans_argument_main_lit_la_ligne_de_commande_du_processus(env, capsys, monkeypatch):
    """``main()`` sans liste analyse ``sys.argv`` : une source inconnue y est refusée (code 2)."""
    monkeypatch.setattr(sys, "argv", ["producteur", "--source", "ftp"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "invalid choice: 'ftp'" in capsys.readouterr().err
    assert env.journal == []


def test_un_sel_de_hachage_absent_en_production_arrete_le_producteur_avec_le_code_2(
    env, evenements, monkeypatch
):
    """Sans clé de hachage on ne masque rien : sortie 2, aucune file créée, aucun démarrage."""
    monkeypatch.setenv("FRAUDE_ENV", "prod")
    monkeypatch.delenv("FRAUDE_SEL_HACHAGE")

    def file_interdite(config):
        raise AssertionError("la file ne doit pas être construite sans clé de hachage")

    monkeypatch.setattr(producteur_module, "construire_file", file_interdite)
    code_sortie = main(["--source", "jeu"])
    assert code_sortie == 2
    assert evenements.tous() == [
        (
            "ERROR",
            "configuration_invalide",
            {
                "cause": "FRAUDE_SEL_HACHAGE (ou FRAUDE_SEL_HACHAGE_FICHIER) est obligatoire "
                "en production."
            },
        )
    ]
    assert env.journal == [("INFO", "json", None)]
    assert not env.base.exists()


def test_un_environnement_inconnu_est_refuse_avant_tout_demarrage(env, evenements, monkeypatch):
    """``FRAUDE_ENV=staging`` : l'erreur de configuration remonte, rien n'est configuré ni créé."""
    monkeypatch.setenv("FRAUDE_ENV", "staging")
    with pytest.raises(ErreurConfiguration) as exc:
        main(["--source", "jeu"])
    assert str(exc.value) == "Environnement inconnu : 'staging'. Valeurs admises : dev, prod."
    assert env.journal == []
    assert env.signaux == []
    assert evenements.tous() == []
    assert not env.donnees.exists()


def test_une_file_injoignable_arrete_le_producteur_avant_de_le_declarer_demarre(
    env, evenements, monkeypatch
):
    """Si la file ne se construit pas, ni démarrage ni arrêt ne sont journalisés."""

    def file_en_panne(config):
        raise OSError("courtier injoignable")

    monkeypatch.setattr(producteur_module, "construire_file", file_en_panne)
    with pytest.raises(OSError) as exc:
        main(["--source", "jeu"])
    assert str(exc.value) == "courtier injoignable"
    assert evenements.tous() == []


def test_une_erreur_pendant_le_rejeu_ferme_quand_meme_la_file_et_journalise_l_arret(
    env, evenements, monkeypatch
):
    """Le bloc ``finally`` ferme la file et trace l'arrêt même quand le rejeu plante."""
    file = FileEspion()
    monkeypatch.setattr(producteur_module, "construire_file", lambda config: file)

    def rejeu_qui_plante(*args, **options):
        raise RuntimeError("rejeu interrompu")

    monkeypatch.setattr(producteur_module, "rejouer_jeu", rejeu_qui_plante)
    with pytest.raises(RuntimeError) as exc:
        main(["--source", "jeu"])
    assert str(exc.value) == "rejeu interrompu"
    assert file.fermetures == 1
    assert [(niveau, nom) for niveau, nom, _ in evenements.tous()] == [
        ("INFO", "producteur_demarre"),
        ("INFO", "producteur_arrete"),
    ]


def test_une_erreur_pendant_la_boucle_api_ferme_quand_meme_la_file_et_journalise_l_arret(
    env, evenements, monkeypatch
):
    """Même garantie en mode API : la file est fermée une fois et l'arrêt est tracé."""
    file = FileEspion()
    monkeypatch.setattr(producteur_module, "construire_file", lambda config: file)

    def boucle_qui_plante(*args, **options):
        raise RuntimeError("boucle interrompue")

    monkeypatch.setattr(producteur_module, "boucle_api", boucle_qui_plante)
    with pytest.raises(RuntimeError) as exc:
        main(["--source", "api"])
    assert str(exc.value) == "boucle interrompue"
    assert file.fermetures == 1
    assert [nom for _, nom, _ in evenements.tous()] == ["producteur_demarre", "producteur_arrete"]


def test_rebaser_sans_jour_remonte_l_erreur_ferme_la_file_et_ne_publie_rien(
    env, evenements, monkeypatch
):
    """``--rebaser-sur`` sans ``--jour`` est refusé ; la file est fermée et la base reste vide."""
    ecrire_jeu(env.jeu, DATES_JEU)
    FileSuivie.instances = []
    monkeypatch.setattr(
        producteur_module, "construire_file", lambda config: FileSuivie(config.file.chemin_sqlite)
    )
    with pytest.raises(ValueError) as exc:
        main(["--source", "jeu", "--rebaser-sur", "2026-10-10"])
    assert str(exc.value) == "rebaser_sur exige de préciser le jour rejoué (--jour)."
    (file,) = FileSuivie.instances
    assert file.fermetures == 1
    assert messages_en_base(env.base) == []
    assert [nom for _, nom, _ in evenements.tous()] == ["producteur_demarre", "producteur_arrete"]


# --------------------------------------------------------------------------------------------
# main : parcours complets
# --------------------------------------------------------------------------------------------
def horodatage(texte: str) -> int:
    return int(datetime.fromisoformat(texte).replace(tzinfo=UTC).timestamp())


@pytest.mark.parametrize(
    ("argv", "valides", "rejetes", "premier_horodatage"),
    [
        ([], list(range(12)), [], None),
        (["--nombre", "5"], list(range(5)), [], None),
        (["--jour", "2026-10-10"], [8, 9, 10, 11], [], None),
        (["--jour", "2026-10-10", "--nombre", "3"], [8, 9, 10], [], None),
        (["--nombre", "10", "--corruptions", "3"], [0, 1, 3, 4, 7, 8, 9], [2, 5, 6], None),
        (["--nombre", "10", "--corruptions", "2"], [0, 1, 3, 4, 6, 7, 8, 9], [2, 5], None),
        (
            ["--nombre", "10", "--corruptions", "2", "--graine", "8"],
            [0, 1, 2, 4, 6, 7, 8, 9],
            [3, 5],
            None,
        ),
        (
            ["--jour", "2026-10-09", "--nombre", "2", "--rebaser-sur", "2026-10-05"],
            [0, 1],
            [],
            horodatage("2026-10-05 10:00:00"),
        ),
    ],
)
def test_main_en_mode_jeu_publie_masque_et_envoie_les_lignes_cassees_a_la_dlq(
    env, evenements, argv, valides, rejetes, premier_horodatage
):
    """Rejeu bout en bout : lignes saines masquées dans les valides, cassées dans les rejets."""
    ecrire_jeu(env.jeu, DATES_JEU)
    jeu_avant = env.jeu.read_bytes()
    assert main(["--source", "jeu", *argv]) == 0
    assert env.jeu.read_bytes() == jeu_avant
    messages = messages_en_base(env.base)
    publies = [valeur for sujet, _, valeur in messages if sujet == "transactions.valides"]
    refuses = [valeur for sujet, _, valeur in messages if sujet == "transactions.rejets"]
    assert [m["trans_num"] for m in publies] == [ligne_brute(n)["trans_num"] for n in valides]
    assert [m["trans_num"] for m in refuses] == [ligne_brute(n)["trans_num"] for n in rejetes]
    assert {m["source"] for m in publies} <= {"jeu"}
    assert {len(m["carte_hash"]) for m in publies} <= {24}
    if premier_horodatage is not None:
        assert publies[0]["horodatage_s"] == premier_horodatage
    lues = len(valides) + len(rejetes)
    assert evenements.nommes("rejeu_termine") == [
        (
            "INFO",
            {"lues": lues, "publiees": len(valides), "rejetees": len(rejetes), "doublons": 0},
        )
    ]
    assert env.journal == [("WARNING", "json", env.donnees / "journaux" / "audit.jsonl")]
    assert [nom for _, nom, _ in evenements.tous() if nom.startswith("producteur_")] == [
        "producteur_demarre",
        "producteur_arrete",
    ]
    assert evenements.nommes("producteur_demarre") == [
        ("INFO", {"source": "jeu", "environnement": "dev", "file": "sqlite"})
    ]


@pytest.mark.parametrize("pannes", [0, 2])
def test_main_en_mode_api_publie_les_lignes_recues_et_survit_aux_pannes_simulees(
    env, evenements, monkeypatch, pannes
):
    """API bout en bout : deux appels, relances si pannes simulées, état de santé écrit."""
    get = GetScripte(reponse_api([ligne_brute(1), ligne_brute(2)]), reponse_api([ligne_brute(3)]))
    monkeypatch.setattr(requests, "get", get)
    argv = ["--source", "api", "--nombre", "2", "--intervalle", "0"]
    if pannes:
        argv += ["--pannes", str(pannes)]
    assert main(argv) == 0
    assert get.appels == [((URL_API,), {"timeout": 4.5})] * 2
    messages = messages_en_base(env.base)
    assert [m["trans_num"] for _, _, m in messages] == [
        ligne_brute(n)["trans_num"] for n in (1, 2, 3)
    ]
    assert {m["source"] for _, _, m in messages} == {"api"}
    assert evenements.nommes("producteur_demarre") == [
        ("INFO", {"source": "api", "environnement": "dev", "file": "sqlite"})
    ]
    assert evenements.nommes("cycle_termine") == [
        ("INFO", {"lues": 2, "publiees": 2, "rejetees": 0, "doublons": 0}),
        ("INFO", {"lues": 1, "publiees": 1, "rejetees": 0, "doublons": 0}),
    ]
    relances = [champs for _, champs in evenements.nommes("api_relance")]
    assert relances == [
        {"tentative": n, "sur": 3, "cause": "ConnectionError", "attente_s": 0.0}
        for n in range(1, pannes + 1)
    ]
    etat = json.loads((env.donnees / "etat" / "producteur.json").read_text(encoding="utf-8"))
    assert etat["echecs_consecutifs"] == 0
    assert (etat["lues"], etat["publiees"]) == (3, 3)
    assert [arret.attentes for arret in env.arrets] == [[0.0] * (pannes + 1)]


def brancher_espions(env, monkeypatch, argv, source_env=None):
    """Remplace les deux modes de production par des espions ; renvoie ce qu'ils ont reçu."""
    if source_env:
        monkeypatch.setenv("FRAUDE_TYPE_SOURCE", source_env)
    recus: dict[str, Any] = {"producteurs": []}

    class ProducteurEspion(Producteur):
        def __init__(self, *args, **options):
            recus["producteurs"].append((args, options))
            super().__init__(*args, **options)

    monkeypatch.setattr(producteur_module, "Producteur", ProducteurEspion)
    monkeypatch.setattr(
        producteur_module,
        "rejouer_jeu",
        lambda *args, **options: recus.update(jeu=(args, options)),
    )
    monkeypatch.setattr(
        producteur_module, "boucle_api", lambda *args, **options: recus.update(api=(args, options))
    )
    assert main(argv) == 0
    return recus


@pytest.mark.parametrize(
    ("argv", "source_env", "options_jeu"),
    [
        (["--source", "jeu"], None, {}),
        (
            [
                "--source",
                "jeu",
                "--jour",
                "2026-10-09",
                "--nombre",
                "12",
                "--corruptions",
                "3",
                "--graine",
                "9",
                "--cadence",
                "2.5",
                "--rebaser-sur",
                "2026-10-12",
            ],
            None,
            {
                "jour": "2026-10-09",
                "limite": 12,
                "corruptions": 3,
                "graine": 9,
                "cadence": 2.5,
                "rebaser_sur": "2026-10-12",
            },
        ),
        (["--nombre", "3"], "jeu", {"limite": 3}),
        (["--source", "jeu"], "api", {}),
    ],
)
def test_main_transmet_les_options_du_mode_jeu_et_installe_les_gestionnaires_d_arret(
    env, monkeypatch, argv, source_env, options_jeu
):
    """Chaque option atterrit dans le bon paramètre de ``rejouer_jeu`` ; les défauts sont fixés."""
    recus = brancher_espions(env, monkeypatch, argv, source_env)
    assert "api" not in recus
    (args, options) = recus["jeu"]
    attendu = {
        "jour": None,
        "limite": None,
        "corruptions": 0,
        "graine": 7,
        "cadence": 0.0,
        "rebaser_sur": None,
        **options_jeu,
    }
    arreter = options.pop("arreter")
    assert options == attendu
    assert type(options["cadence"]) is float
    config, producteur = args
    assert config.source.jeu_chemin == env.jeu
    assert isinstance(producteur, Producteur)
    ((args_producteur, options_producteur),) = recus["producteurs"]
    assert options_producteur == {"source": "jeu"}
    assert args_producteur[0] is config
    hacheur = args_producteur[2]
    assert hacheur.empreinte(4929) == HacheurCarte(SEL.encode(), 24).empreinte(4929)
    assert [numero for numero, _ in env.signaux] == [signal_reel.SIGINT, signal_reel.SIGTERM]
    assert not arreter.is_set()
    env.signaux[0][1](signal_reel.SIGINT, None)
    assert arreter.is_set()
    arreter.clear()
    env.signaux[1][1](signal_reel.SIGTERM, None)
    assert arreter.is_set()

    # Une source inconnue dans la configuration est refusée avant tout démarrage de mode.
    monkeypatch.setenv("FRAUDE_TYPE_SOURCE", "ftp")
    appels_avant = (recus.get("jeu"), recus.get("api"), list(recus["producteurs"]))
    with pytest.raises(ErreurConfiguration) as exc:
        main(["--nombre", "3"])
    assert str(exc.value) == "Type de source inconnu : 'ftp'."
    assert (recus.get("jeu"), recus.get("api"), list(recus["producteurs"])) == appels_avant


@pytest.mark.parametrize(
    ("argv", "source_env", "nombre", "intervalle", "pannes"),
    [
        ([], None, None, None, 0),
        (["--source", "api"], "jeu", None, None, 0),
        (["--source", "api", "--nombre", "4", "--intervalle", "1.5"], None, 4, 1.5, 0),
        (["--source", "api", "--pannes", "3"], None, None, None, 3),
    ],
)
def test_main_transmet_les_options_du_mode_api_et_enveloppe_les_pannes_simulees(
    env, monkeypatch, argv, source_env, nombre, intervalle, pannes
):
    """Source par défaut = configuration (api) ; ``--pannes`` enveloppe l'appel HTTP réel."""
    appel_http = GetScripte(Reponse(200, "[]"))
    monkeypatch.setattr(requests, "get", appel_http)
    recus = brancher_espions(env, monkeypatch, argv, source_env)
    assert "jeu" not in recus
    (args, options) = recus["api"]
    config, producteur, arreter = args
    assert config.source.api_url == URL_API
    assert options["nombre_appels"] == nombre
    assert options["intervalle_s"] == intervalle
    assert set(options) == {"nombre_appels", "intervalle_s", "get"}
    assert recus["producteurs"][0][1] == {"source": "api"}
    get = options["get"]
    if pannes == 0:
        assert get is appel_http
    else:
        assert isinstance(get, PannesSimulees)
        for _ in range(pannes):
            with pytest.raises(requests.ConnectionError):
                get(URL_API)
        assert appel_http.appels == []
        assert get(URL_API).status_code == 200
    env.signaux[1][1](signal_reel.SIGTERM, None)
    assert arreter.is_set()
