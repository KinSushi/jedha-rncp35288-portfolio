# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests INVERSES de l'orchestration (``kayak.pipeline``) : ce qu'il refuse ou laisse intact.

Le pipeline enchaîne des étapes qui lisent leurs entrées dans le data lake. Ce fichier prouve
qu'il refuse de continuer quand une entrée manque (table absente, CSV vide, partie Parquet
corrompue, données d'une autre date), qu'il s'arrête à la première étape en échec tout en
refermant le journal HTTP, qu'il n'ouvre ce journal que pour les étapes de collecte, qu'il
n'écrit rien lui-même dans le lake, et que la ligne de commande rejette toute étape ou option
inconnue avec un code de sortie 2.

Aucun réseau, aucun Spark : chaque étape importée à la demande par ``executer`` (collecteurs,
ETL, cartes) est remplacée par une fausse étape qui note ses appels ; ces faux modules sont
posés dans ``sys.modules``, donc ni scrapy, ni pyspark, ni plotly ne sont même importés.
"""

import contextlib
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from kayak import config, pipeline
from kayak.stockage.data_lake import DataLake

DATE = "2026-10-10"
AUTRE_DATE = "2026-10-09"
BASE = f"date={DATE}"
CHEMIN_DESTINATIONS = f"raw/destinations/{BASE}/destinations.csv"
CHEMIN_COORDONNEES = f"raw/geocodage/{BASE}/coordonnees.csv"
CHEMIN_BOOKING = f"raw/booking/{BASE}/hotels.jsonl"
CHEMIN_VILLES = f"curated/villes/{BASE}/villes.parquet"
CHEMIN_ENRICHI = f"curated/kayak_enrichi/{BASE}/kayak_enrichi.parquet"

NOMS_DES_VILLES = {1: "Nîmes", 2: "Lille", 3: "Annecy"}
DESTINATIONS = pd.DataFrame({"id_ville": [1, 2, 3], "ville": list(NOMS_DES_VILLES.values())})
COORDONNEES = pd.DataFrame(
    {
        "id_ville": [1, 2, 3],
        "ville": list(NOMS_DES_VILLES.values()),
        "latitude": [43.84, 50.63, 45.9],
        "longitude": [4.36, 3.06, 6.13],
    }
)
VILLES = pd.DataFrame(
    {"id_ville": [1, 2, 3], "ville": list(NOMS_DES_VILLES.values()), "rang_meteo": [1, 2, 3]}
)
ENRICHI = pd.DataFrame(
    {
        "hotel_id": [10, 11, 12, 13],
        "id_ville": [1, 1, 2, 3],
        "nom": ["H1", "H2", "H3", "H4"],
        "rang_meteo": [1, 5, 6, 12],  # le rang 5 est retenu, le rang 6 est le premier exclu
    }
)
ETAPES_DE_COLLECTE = ["destinations", "geocodage", "meteo", "booking", "osm"]


def _csv(tableau: pd.DataFrame) -> bytes:
    """Octets d'un CSV tel que la collecte le dépose dans le lake."""
    return tableau.to_csv(index=False).encode("utf-8")


def _parquet(tableau: pd.DataFrame) -> bytes:
    """Octets d'un fichier Parquet (une « partie » écrite par Spark)."""
    return tableau.to_parquet()


def _instantane(racine: Path) -> dict[str, bytes]:
    """Photographie du lake : chemin relatif -> contenu, pour prouver qu'il n'a pas changé."""
    return {
        fichier.relative_to(racine).as_posix(): fichier.read_bytes()
        for fichier in racine.rglob("*")
        if fichier.is_file()
    }


class Banc:
    """Fausses étapes : elles notent chaque appel (nom, arguments) et rendent un résultat connu.

    ``erreurs`` associe le nom d'une étape à l'exception qu'elle doit lever. Les arguments
    notés sont simplifiés pour être comparables : un tableau devient un dictionnaire de listes,
    le data lake devient le mot « lake ».
    """

    def __init__(self, lake: DataLake) -> None:
        self.lake = lake
        self.appels: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.erreurs: dict[str, Exception] = {}
        self.diagnostic: dict[str, Any] = {"villes_plafonnees": 0, "plafond": 40}
        self.ouvertures: list[DataLake] = []
        self.fermetures: list[DataLake] = []

    @property
    def noms(self) -> list[str]:
        """Noms des étapes appelées, dans l'ordre."""
        return [nom for nom, _, _ in self.appels]

    def _simplifier(self, valeur: Any) -> Any:
        if isinstance(valeur, pd.DataFrame):
            return valeur.to_dict("list")
        if valeur is self.lake:
            return "lake"
        return valeur

    def etape(self, nom: str, retour: Callable[..., Any] | None = None) -> Callable[..., Any]:
        """Fabrique une fausse étape appelable sous le nom ``nom``."""

        def appeler(*args: Any, **kwargs: Any) -> Any:
            self.appels.append(
                (
                    nom,
                    tuple(self._simplifier(a) for a in args),
                    {cle: self._simplifier(v) for cle, v in kwargs.items()},
                )
            )
            if nom in self.erreurs:
                raise self.erreurs[nom]
            return retour(*args) if retour else f"résultat-{nom}"

        return appeler

    @contextlib.contextmanager
    def journaliser(self, lake: DataLake) -> Iterator[list[dict[str, Any]]]:
        """Faux ``journaliser_requetes`` : note l'ouverture puis la fermeture du bloc."""
        self.ouvertures.append(lake)
        try:
            yield []
        finally:
            self.fermetures.append(lake)


@pytest.fixture(autouse=True)
def _date_fixee(monkeypatch):
    """Date de collecte déterministe, restaurée à la fin de chaque test."""
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", DATE)


@pytest.fixture
def lake(tmp_path: Path) -> DataLake:
    """Data lake local, vide, isolé dans ``tmp_path``."""
    return DataLake(bucket=None, racine_locale=tmp_path)


@pytest.fixture
def lake_pret(lake: DataLake) -> DataLake:
    """Lake contenant les entrées de toutes les étapes, pour la date DATE (et elle seule)."""
    lake.ecrire(CHEMIN_DESTINATIONS, _csv(DESTINATIONS))
    lake.ecrire(CHEMIN_COORDONNEES, _csv(COORDONNEES))
    lake.ecrire(f"{CHEMIN_VILLES}/part-00000.parquet", _parquet(VILLES))
    lake.ecrire(f"{CHEMIN_ENRICHI}/part-00000.parquet", _parquet(ENRICHI))
    return lake


@pytest.fixture
def banc(monkeypatch, lake: DataLake) -> Banc:
    """Remplace le lake, le journal HTTP et toutes les étapes importées à la demande."""
    banc = Banc(lake)
    modules: dict[str, dict[str, Callable[..., Any]]] = {
        "kayak.collecte.destinations": {"collecter_destinations": banc.etape("destinations")},
        "kayak.collecte.geocodage": {"collecter_coordonnees": banc.etape("geocodage")},
        "kayak.collecte.meteo": {"collecter_meteo": banc.etape("meteo")},
        "kayak.collecte.booking_spider": {"lancer_collecte_booking": banc.etape("booking")},
        "kayak.collecte.hotels_osm": {
            "collecter_hotels_osm": banc.etape("osm"),
            "diagnostic_troncature": banc.etape("diagnostic", lambda lac: banc.diagnostic),
        },
        "kayak.etl.spark_etl": {"executer_etl": banc.etape("etl")},
        "kayak.cartes": {
            "carte_destinations": banc.etape("carte_destinations", lambda villes: "figure-d"),
            "carte_hotels": banc.etape("carte_hotels", lambda *args, **kwargs: "figure-h"),
            "enregistrer": banc.etape("enregistrer", lambda figure, nom: f"fichier-{nom}"),
        },
    }
    for nom, attributs in modules.items():
        faux = ModuleType(nom)
        for attribut, fonction in attributs.items():
            setattr(faux, attribut, fonction)
        monkeypatch.setitem(sys.modules, nom, faux)
    monkeypatch.setattr(pipeline, "DataLake", lambda: lake)
    monkeypatch.setattr(pipeline, "journaliser_requetes", banc.journaliser)
    return banc


# --------------------------------------------------------------------------------------------
# Lecture dans le lake : refus
# --------------------------------------------------------------------------------------------


def test_lire_csv_fichier_absent_leve_filenotfound(lake):
    """Un CSV qui n'existe pas dans le lake est une erreur, jamais un tableau vide."""
    with pytest.raises(FileNotFoundError, match="absent.csv"):
        pipeline.lire_csv(lake, "raw/absent.csv")


def test_lire_csv_vide_est_refuse(lake):
    """Un CSV vide (collecte interrompue) est refusé : pas de colonnes, donc pas de tableau."""
    lake.ecrire("raw/vide.csv", b"")
    with pytest.raises(pd.errors.EmptyDataError, match="No columns to parse"):
        pipeline.lire_csv(lake, "raw/vide.csv")


@pytest.mark.parametrize(
    "fichiers",
    [
        [],
        ["_SUCCESS", ".part-00000.parquet.crc", "part-00000.parquet.crc", "notes.txt"],
    ],
    ids=["dossier_absent", "aucune_partie_parquet"],
)
def test_lire_parquet_sans_partie_parquet_leve_une_erreur_explicite(lake, fichiers):
    """Sans fichier ``.parquet`` (marqueurs et sommes de contrôle exclus) : FileNotFoundError."""
    dossier = "curated/t/date=2026-10-10/t.parquet"
    for nom in fichiers:
        lake.ecrire(f"{dossier}/{nom}", b"pas du parquet")
    message = rf"^aucun fichier Parquet sous {re.escape(dossier)}$"
    with pytest.raises(FileNotFoundError, match=message):
        pipeline.lire_parquet(lake, dossier)


def test_lire_parquet_partie_corrompue_n_est_pas_ignoree(lake):
    """Une partie illisible fait échouer la lecture : on ne rend jamais une table amputée."""
    dossier = "curated/t/t.parquet"
    lake.ecrire(f"{dossier}/part-00000.parquet", _parquet(pd.DataFrame({"a": [1]})))
    lake.ecrire(f"{dossier}/part-00001.parquet", b"octets qui ne sont pas du Parquet")
    with pytest.raises(pa.ArrowInvalid, match="Parquet"):
        pipeline.lire_parquet(lake, dossier)


def test_lire_parquet_filtre_sur_une_colonne_inconnue_est_refuse(lake):
    """Le filtre est transmis à pyarrow : une colonne qui n'existe pas est une erreur."""
    lake.ecrire("curated/t/t.parquet/part-00000.parquet", _parquet(pd.DataFrame({"a": [1, 2]})))
    with pytest.raises(pa.ArrowInvalid, match="zzz"):
        pipeline.lire_parquet(lake, "curated/t/t.parquet", filtres=[("zzz", "<=", 5)])


def test_lire_parquet_ignore_un_dossier_voisin_au_nom_proche(lake):
    """Lire ``t.parquet`` ne mélange pas les parties de ``t.parquet.old``, resté intact."""
    lake.ecrire("curated/t.parquet/part-00000.parquet", _parquet(pd.DataFrame({"a": [1]})))
    lake.ecrire("curated/t.parquet.old/part-00000.parquet", _parquet(pd.DataFrame({"a": [99]})))
    assert pipeline.lire_parquet(lake, "curated/t.parquet")["a"].tolist() == [1]
    assert lake.existe("curated/t.parquet.old/part-00000.parquet")


@pytest.mark.parametrize("lecture", ["csv", "parquet"])
def test_lire_ne_modifie_pas_le_lake(lake, tmp_path, lecture):
    """Lire une table est sans écriture : le lake reste identique, octet pour octet."""
    lake.ecrire("raw/t.csv", _csv(DESTINATIONS))
    lake.ecrire("curated/t.parquet/part-00000.parquet", _parquet(VILLES))
    avant = _instantane(tmp_path)
    if lecture == "csv":
        pipeline.lire_csv(lake, "raw/t.csv")
    else:
        pipeline.lire_parquet(lake, "curated/t.parquet", filtres=[("rang_meteo", "<=", 2)])
    assert _instantane(tmp_path) == avant


def test_lire_parquet_laisse_la_liste_de_filtres_inchangee(lake):
    """Les filtres de l'appelant sont transmis tels quels, sans être modifiés au passage."""
    lake.ecrire("curated/t.parquet/part-00000.parquet", _parquet(VILLES))
    filtres = [("rang_meteo", "<=", 2)]
    filtres_avant = list(filtres)
    pipeline.lire_parquet(lake, "curated/t.parquet", filtres=filtres)
    assert filtres == filtres_avant


def test_lire_parquet_assemble_les_parties_dans_l_ordre_et_filtre_chaque_partie(lake):
    """Parties lues dans l'ordre des noms, index continu, filtre appliqué à chaque partie."""
    dossier = "curated/t/t.parquet"
    lake.ecrire(
        f"{dossier}/part-00001.parquet",
        _parquet(pd.DataFrame({"rang": [9, 2], "nom": ["i", "b"]})),
    )
    lake.ecrire(
        f"{dossier}/part-00000.parquet",
        _parquet(pd.DataFrame({"rang": [1, 8], "nom": ["a", "h"]})),
    )
    lake.ecrire(f"{dossier}/_SUCCESS", b"marqueur Spark, pas du Parquet")
    lake.ecrire(f"{dossier}/.part-00000.parquet.crc", b"somme Hadoop, pas du Parquet")
    tout = pipeline.lire_parquet(lake, dossier)
    assert tout["nom"].tolist() == ["a", "h", "i", "b"]
    assert tout.index.tolist() == [0, 1, 2, 3]
    filtre = pipeline.lire_parquet(lake, dossier, filtres=[("rang", "<=", 5)])
    assert filtre["nom"].tolist() == ["a", "b"]
    assert filtre.index.tolist() == [0, 1]


# --------------------------------------------------------------------------------------------
# Date de collecte et journal
# --------------------------------------------------------------------------------------------


def test_executer_pose_la_date_dans_l_environnement_et_la_journalise(banc, caplog):
    """La date donnée devient celle de toutes les étapes (variable d'environnement), tracée."""
    caplog.set_level(logging.INFO, logger="kayak")
    pipeline.executer([], "2026-12-25")
    assert os.environ["KAYAK_DATE_COLLECTE"] == "2026-12-25"
    assert config.date_collecte() == "2026-12-25"
    messages = [
        (r.levelno, r.getMessage()) for r in caplog.records if r.name == "kayak"
    ]
    assert messages == [(logging.INFO, "Date de collecte : 2026-12-25 ; data lake : local")]


@pytest.mark.parametrize("date", [None, ""], ids=["aucune_date", "date_vide"])
def test_executer_sans_date_laisse_l_environnement_inchange(banc, caplog, date):
    """Sans date explicite (None ou ""), la variable d'environnement existante est conservée."""
    caplog.set_level(logging.INFO, logger="kayak")
    resultats = pipeline.executer([], date)
    assert os.environ["KAYAK_DATE_COLLECTE"] == DATE
    assert resultats == {}
    assert banc.appels == []
    assert [r.getMessage() for r in caplog.records if r.name == "kayak"] == [
        f"Date de collecte : {DATE} ; data lake : local"
    ]


@pytest.mark.parametrize("etapes", [[], ["etl"], ["cartes"], ["etl", "cartes"]])
def test_executer_sans_etape_de_collecte_n_ouvre_pas_le_journal_http_et_laisse_le_lake_intact(
    banc, lake_pret, tmp_path, etapes
):
    """ETL et cartes ne font aucune requête web : pas de journal HTTP, lake inchangé."""
    avant = _instantane(tmp_path)
    pipeline.executer(etapes, DATE)
    assert banc.ouvertures == []
    assert banc.fermetures == []
    assert _instantane(tmp_path) == avant


@pytest.mark.parametrize("etape", pipeline.ETAPES)
def test_executer_n_ecrit_rien_lui_meme_dans_le_lake(banc, lake_pret, tmp_path, etape):
    """L'orchestrateur ne fait que lire : seules les étapes (ici factices) écrivent au lake."""
    avant = _instantane(tmp_path)
    pipeline.executer([etape], DATE)
    assert _instantane(tmp_path) == avant


def test_executer_ferme_le_journal_http_meme_si_une_etape_echoue(banc, lake):
    """Le journal HTTP est refermé (donc archivé) même quand une étape de collecte plante."""
    banc.erreurs["destinations"] = RuntimeError("la source ne répond plus")
    with pytest.raises(RuntimeError, match="la source ne répond plus"):
        pipeline.executer(["destinations"], DATE)
    assert banc.ouvertures == [lake]
    assert banc.fermetures == [lake]


@pytest.mark.parametrize(
    "etapes, en_echec, appelees",
    [
        (["destinations", "geocodage", "meteo"], "geocodage", ["destinations", "geocodage"]),
        (["etl", "cartes"], "etl", ["etl"]),
        (["osm", "etl"], "osm", ["osm"]),
    ],
    ids=["collecte", "etl_puis_cartes", "osm_puis_etl"],
)
def test_executer_s_arrete_a_la_premiere_etape_en_echec(
    banc, lake_pret, etapes, en_echec, appelees
):
    """Après une étape en échec, les suivantes ne sont jamais lancées ; l'erreur remonte."""
    banc.erreurs[en_echec] = RuntimeError(f"échec de {en_echec}")
    with pytest.raises(RuntimeError, match=f"^échec de {en_echec}$"):
        pipeline.executer(etapes, DATE)
    assert banc.noms == appelees


# --------------------------------------------------------------------------------------------
# Entrées manquantes
# --------------------------------------------------------------------------------------------

ENTREES = [
    ("geocodage", CHEMIN_DESTINATIONS),
    ("meteo", CHEMIN_COORDONNEES),
    ("booking", CHEMIN_DESTINATIONS),
    ("osm", CHEMIN_COORDONNEES),
]


@pytest.mark.parametrize("etape, entree", ENTREES, ids=[e for e, _ in ENTREES])
def test_executer_etape_sans_entree_leve_filenotfound_et_n_appelle_pas_le_collecteur(
    banc, lake, etape, entree
):
    """Étape lancée sans celle qui la précède : erreur nette, collecteur jamais appelé."""
    with pytest.raises(FileNotFoundError) as erreur:
        pipeline.executer([etape], DATE)
    assert Path(erreur.value.filename).as_posix().endswith(entree)
    assert banc.noms == []
    assert banc.fermetures == [lake]


@pytest.mark.parametrize("etape, entree", ENTREES, ids=[e for e, _ in ENTREES])
def test_executer_ne_lit_pas_les_donnees_d_une_autre_date(banc, lake_pret, etape, entree):
    """Les données archivées pour DATE ne servent pas à rejouer une autre date."""
    with pytest.raises(FileNotFoundError) as erreur:
        pipeline.executer([etape], AUTRE_DATE)
    attendu = entree.replace(BASE, f"date={AUTRE_DATE}")
    assert Path(erreur.value.filename).as_posix().endswith(attendu)
    assert banc.noms == []


@pytest.mark.parametrize("manquante", ["villes", "enrichi"])
def test_executer_cartes_sans_table_curated_n_enregistre_aucune_carte(banc, lake, manquante):
    """« cartes » seul exige les deux tables de l'ETL : sinon rien n'est tracé ni enregistré."""
    tables = {"villes": CHEMIN_VILLES, "enrichi": CHEMIN_ENRICHI}
    presente = next(chemin for nom, chemin in tables.items() if nom != manquante)
    lake.ecrire(f"{presente}/part-00000.parquet", _parquet(VILLES))
    message = rf"^aucun fichier Parquet sous {re.escape(tables[manquante])}$"
    with pytest.raises(FileNotFoundError, match=message):
        pipeline.executer(["cartes"], DATE)
    assert banc.noms == []


@pytest.mark.parametrize(
    "en_echec, appelees",
    [
        ("diagnostic", ["diagnostic"]),
        ("carte_destinations", ["diagnostic", "carte_destinations"]),
    ],
)
def test_executer_cartes_en_echec_n_enregistre_aucune_carte(banc, lake_pret, en_echec, appelees):
    """Si le diagnostic ou la première carte échoue, rien n'est enregistré ensuite."""
    banc.erreurs[en_echec] = RuntimeError("échec simulé")
    with pytest.raises(RuntimeError, match="^échec simulé$"):
        pipeline.executer(["cartes"], DATE)
    assert banc.noms == appelees


# --------------------------------------------------------------------------------------------
# Indépendance des appels et des arguments
# --------------------------------------------------------------------------------------------


def test_executer_ne_modifie_ni_la_liste_demandee_ni_les_etapes_par_defaut(banc, lake_pret):
    """La liste d'étapes de l'appelant et la constante ETAPES restent inchangées."""
    demandees = ["osm", "etl"]
    demandees_avant = list(demandees)
    etapes_avant = list(pipeline.ETAPES)
    pipeline.executer(demandees, DATE)
    pipeline.executer(pipeline.ETAPES, DATE)
    assert demandees == demandees_avant
    assert pipeline.ETAPES == etapes_avant


def test_executer_deux_appels_successifs_laissent_le_premier_resultat_inchange(banc, lake_pret):
    """Chaque exécution rend son propre dictionnaire : le second appel ne touche pas au premier."""
    premier = pipeline.executer(["destinations"], DATE)
    second = pipeline.executer(["meteo"], DATE)
    assert premier == {"destinations": "résultat-destinations"}
    assert second == {"meteo": "résultat-meteo"}
    assert second is not premier


# --------------------------------------------------------------------------------------------
# Ligne de commande
# --------------------------------------------------------------------------------------------


class Commande:
    """Lance ``pipeline.main`` avec des arguments donnés, ``executer`` étant remplacé.

    ``appels`` note les arguments reçus par ``executer`` ; ``configurations`` les appels à
    ``logging.basicConfig`` ; ``valeur`` est ce que ``executer`` rend, ``erreur`` ce qu'il lève.
    """

    def __init__(self, monkeypatch) -> None:
        self._monkeypatch = monkeypatch
        self.appels: list[tuple[list[str], str | None]] = []
        self.configurations: list[dict[str, Any]] = []
        self.valeur: dict[str, Any] = {}
        self.erreur: Exception | None = None
        monkeypatch.setattr(pipeline, "executer", self._executer)
        monkeypatch.setattr(logging, "basicConfig", self._configurer_journal)

    def _executer(self, etapes: list[str], date: str | None = None) -> dict[str, Any]:
        self.appels.append((etapes, date))
        if self.erreur:
            raise self.erreur
        return self.valeur

    def _configurer_journal(self, **options: Any) -> None:
        self.configurations.append(options)

    def __call__(self, *arguments: str) -> None:
        self._monkeypatch.setattr(sys, "argv", ["pipeline", *arguments])
        pipeline.main()


@pytest.fixture
def commande(monkeypatch) -> Commande:
    """Commande en ligne de commande dont l'exécution réelle du pipeline est remplacée."""
    return Commande(monkeypatch)


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        (["--etapes", "inconnue"], "invalid choice: 'inconnue'"),
        (["--etapes"], "expected at least one argument"),
        (["--date"], "expected one argument"),
        (["--option-inconnue"], "unrecognized arguments: --option-inconnue"),
        (["etl"], "unrecognized arguments: etl"),
    ],
    ids=["etape_inconnue", "etapes_vide", "date_vide", "option_inconnue", "positionnel"],
)
def test_main_refuse_une_ligne_de_commande_invalide_avec_le_code_de_sortie_2(
    commande, capsys, arguments, fragment
):
    """Étape ou option inconnue, valeur manquante : SystemExit(2), rien n'est exécuté."""
    with pytest.raises(SystemExit) as sortie:
        commande(*arguments)
    assert sortie.value.code == 2
    assert fragment in capsys.readouterr().err
    assert commande.appels == []


def test_lancement_en_module_refuse_une_etape_inconnue_avec_un_code_de_sortie_non_nul():
    """``python -m kayak.pipeline`` appelle bien ``main`` : une étape inconnue sort en erreur."""
    resultat = subprocess.run(
        [sys.executable, "-m", "kayak.pipeline", "--etapes", "inconnue"],
        cwd=config.RACINE,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert resultat.returncode == 2
    assert resultat.stdout == ""
    assert "invalid choice: 'inconnue'" in resultat.stderr


def test_main_ne_masque_pas_l_echec_de_executer(commande, capsys):
    """Si le pipeline échoue, l'erreur remonte (code de sortie non nul) et rien n'est affiché."""
    commande.erreur = RuntimeError("panne du pipeline")
    with pytest.raises(RuntimeError, match="^panne du pipeline$"):
        commande()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "resultat",
    [{}, {"destinations": "x", "hotels": "y"}, {"cartes": {"destinations": "z"}}],
    ids=["vide", "collecte_seule", "cartes_seules"],
)
def test_main_sans_etl_n_affiche_aucun_controle_la_sortie_reste_intacte(
    commande, capsys, resultat
):
    """Contrôles de qualité affichés seulement si l'ETL a tourné : sinon, aucune ligne émise."""
    commande.valeur = resultat
    commande("--etapes", "cartes")
    sortie = capsys.readouterr()
    assert sortie.out == ""
    assert sortie.err == ""


def test_main_laisse_la_liste_des_etapes_par_defaut_inchangee(commande):
    """Lancer la commande (avec ou sans ``--etapes``) ne modifie pas la constante ETAPES."""
    etapes_avant = list(pipeline.ETAPES)
    commande()
    commande("--etapes", "etl", "cartes")
    assert pipeline.ETAPES == etapes_avant


def test_main_aide_decrit_la_commande_sans_rien_executer(commande, capsys, monkeypatch):
    """``--help`` affiche la description et les options puis s'arrête (code 0), sans étape."""
    monkeypatch.setenv("COLUMNS", "200")  # pas de retour à la ligne dépendant du terminal
    with pytest.raises(SystemExit) as sortie:
        commande("--help")
    assert sortie.value.code == 0
    aide = capsys.readouterr().out
    assert "\n\nPipeline Kayak\n\n" in aide
    assert re.search(r"--date DATE +date de collecte AAAA-MM-JJ\n", aide)
    assert "--etapes {destinations,geocodage,meteo,booking,osm,etl,cartes}" in aide
    assert commande.appels == []
    assert commande.configurations == [
        {"level": logging.INFO, "format": "%(asctime)s %(levelname)s %(message)s"}
    ]


# --------------------------------------------------------------------------------------------
# Tests directs : comportement nominal
# --------------------------------------------------------------------------------------------


def test_constantes_etapes_et_etapes_de_collecte():
    """Les sept étapes dans l'ordre du pipeline ; seules les cinq premières interrogent le web."""
    assert pipeline.ETAPES == [
        "destinations",
        "geocodage",
        "meteo",
        "booking",
        "osm",
        "etl",
        "cartes",
    ]
    assert pipeline.ETAPES_DE_COLLECTE == set(ETAPES_DE_COLLECTE)
    assert set(pipeline.ETAPES) - pipeline.ETAPES_DE_COLLECTE == {"etl", "cartes"}


DEST = DESTINATIONS.to_dict("list")
COORD = COORDONNEES.to_dict("list")
URL_BANC = "sqlite:///banc.db"
RES_DEST, RES_GEO, RES_METEO = "résultat-destinations", "résultat-geocodage", "résultat-meteo"
RES_BOOKING, RES_OSM, RES_ETL = "résultat-booking", "résultat-osm", "résultat-etl"


@pytest.mark.parametrize(
    "etapes, appels, resultats, journal",
    [
        (["destinations"], [("destinations", ("lake",))], {"destinations": RES_DEST}, True),
        (["geocodage"], [("geocodage", (DEST, "lake"))], {"coordonnees": RES_GEO}, True),
        (["meteo"], [("meteo", (COORD, "lake"))], {"meteo": RES_METEO}, True),
        (["booking"], [("booking", (DEST, "lake"))], {"hotels": RES_BOOKING}, True),
        (["osm"], [("osm", (COORD, "lake"))], {"hotels_osm": RES_OSM}, True),
        (["etl"], [("etl", ("lake", DATE, URL_BANC))], {"etl": RES_ETL}, False),
        (
            ["etl", "osm", "meteo", "geocodage", "destinations"],
            [
                ("destinations", ("lake",)),
                ("geocodage", (DEST, "lake")),
                ("meteo", (COORD, "lake")),
                ("osm", (COORD, "lake")),
                ("etl", ("lake", DATE, URL_BANC)),
            ],
            {
                "destinations": RES_DEST,
                "coordonnees": RES_GEO,
                "meteo": RES_METEO,
                "hotels_osm": RES_OSM,
                "etl": RES_ETL,
            },
            True,
        ),
    ],
    ids=["destinations", "geocodage", "meteo", "booking", "osm", "etl", "ordre_du_pipeline"],
)
def test_executer_appelle_chaque_etape_avec_ses_entrees_et_ouvre_le_journal_si_collecte(
    banc, lake_pret, monkeypatch, etapes, appels, resultats, journal
):
    """Chaque étape reçoit son entrée lue dans le lake et son résultat est rangé sous sa clé ;
    l'ordre est celui du pipeline (pas de la demande) ; le journal HTTP suit la collecte."""
    monkeypatch.setattr(config, "DB_URL", URL_BANC)
    obtenus = pipeline.executer(etapes, DATE)
    assert obtenus == resultats
    assert [(nom, args) for nom, args, _ in banc.appels] == appels
    assert banc.ouvertures == ([lake_pret] if journal else [])
    assert banc.fermetures == banc.ouvertures


@pytest.mark.parametrize(
    "booking, manquantes",
    [
        pytest.param(None, [1, 2, 3], id="fichier_absent"),
        pytest.param(b"", [1, 2, 3], id="fichier_vide"),
        pytest.param(b"  \n\n", [1, 2, 3], id="fichier_de_blancs"),
        pytest.param(
            b'{"id_ville": 1, "nom": "H1"}\n{"id_ville": 3, "nom": "H3"}\n{"id_ville": 1}\n',
            [2],
            id="villes_1_et_3_servies",
        ),
        pytest.param(b'{"id_ville": 99, "nom": "H"}\n', [1, 2, 3], id="ville_servie_inconnue"),
        pytest.param(
            b'{"id_ville": 1}\n{"id_ville": 2}\n{"id_ville": 3}\n', [], id="toutes_servies"
        ),
    ],
)
def test_osm_ne_recoit_que_les_villes_que_booking_n_a_pas_servies(
    banc, lake_pret, caplog, booking, manquantes
):
    """Repli OpenStreetMap : seules les villes absentes du fichier Booking sont transmises,
    et ce fichier reste intact (le repli ne réécrit pas l'archive de Booking)."""
    caplog.set_level(logging.INFO, logger="kayak")
    if booking is not None:
        lake_pret.ecrire(CHEMIN_BOOKING, booking)
    pipeline.executer(["osm"], DATE)
    if booking is not None:
        assert lake_pret.lire(CHEMIN_BOOKING) == booking
    ((nom, (recues, lac), _),) = banc.appels
    assert nom == "osm"
    assert lac == "lake"
    assert list(recues) == ["id_ville", "ville", "latitude", "longitude"]
    assert recues["id_ville"] == manquantes
    assert recues["ville"] == [NOMS_DES_VILLES[i] for i in manquantes]
    assert (
        logging.INFO,
        f"Villes sans hôtel Booking : {len(manquantes)} ; repli OpenStreetMap",
    ) in [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "kayak"]


def test_osm_laisse_intact_et_ignore_le_fichier_booking_d_une_autre_date(banc, lake_pret):
    """Un fichier Booking archivé un autre jour ne compte pas (toutes les villes restent) et
    reste intact."""
    archive = b'{"id_ville": 1}\n{"id_ville": 2}\n{"id_ville": 3}\n'
    lake_pret.ecrire(f"raw/booking/date={AUTRE_DATE}/hotels.jsonl", archive)
    pipeline.executer(["osm"], DATE)
    ((_, (recues, _), _),) = banc.appels
    assert recues["id_ville"] == [1, 2, 3]
    assert lake_pret.lire(f"raw/booking/date={AUTRE_DATE}/hotels.jsonl") == archive


@pytest.mark.parametrize(
    "plafonnees, plafond, mention",
    [
        (0, 40, None),
        (1, 40, "20 meilleurs parmi au plus 40 hôtels OSM par ville"),
        (7, 55, "20 meilleurs parmi au plus 55 hôtels OSM par ville"),
    ],
    ids=["aucune_plafonnee", "une_plafonnee", "plusieurs_plafonnees"],
)
def test_executer_cartes_lit_le_curated_filtre_sur_le_top_5_et_enregistre_deux_cartes(
    banc, lake_pret, tmp_path, plafonnees, plafond, mention
):
    """Cartes : tables « curated » lues et laissées intactes, hôtels du top 5 seulement (rang 5
    gardé, 6 écarté), mention de l'échantillon si des villes sont plafonnées, deux cartes."""
    banc.diagnostic = {"villes_plafonnees": plafonnees, "plafond": plafond}
    avant = _instantane(tmp_path)
    resultats = pipeline.executer(["cartes"], DATE)
    assert _instantane(tmp_path) == avant
    assert resultats == {
        "cartes": {
            "destinations": "fichier-top5_destinations",
            "hotels": "fichier-top20_hotels",
        }
    }
    villes = VILLES.to_dict("list")
    (diagnostic, destinations, enreg_1, hotels, enreg_2) = banc.appels
    assert diagnostic == ("diagnostic", ("lake",), {})
    assert destinations == ("carte_destinations", (villes,), {})
    assert enreg_1 == ("enregistrer", ("figure-d", "top5_destinations"), {})
    nom, (enrichi, villes_recues), options = hotels
    assert nom == "carte_hotels"
    assert enrichi["hotel_id"] == [10, 11]
    assert enrichi["rang_meteo"] == [1, 5]
    assert villes_recues == villes
    assert options == {"note_echantillon": mention}
    assert enreg_2 == ("enregistrer", ("figure-h", "top20_hotels"), {})


@pytest.mark.parametrize(
    "arguments, etapes, date",
    [
        ([], pipeline.ETAPES, None),
        (["--date", "2026-10-10"], pipeline.ETAPES, "2026-10-10"),
        (["--etapes", "etl", "cartes"], ["etl", "cartes"], None),
        (["--etapes", "osm", "--date", "2026-01-02"], ["osm"], "2026-01-02"),
    ],
    ids=["defauts", "date_seule", "etapes_seules", "etapes_et_date"],
)
def test_main_transmet_etapes_et_date_a_executer_apres_avoir_configure_le_journal(
    commande, arguments, etapes, date
):
    """Sans option : toutes les étapes, date du jour (None). Le journal est configuré en INFO."""
    commande(*arguments)
    assert commande.appels == [(etapes, date)]
    assert commande.configurations == [
        {"level": logging.INFO, "format": "%(asctime)s %(levelname)s %(message)s"}
    ]


def test_main_affiche_les_controles_de_qualite_et_les_lignes_chargees_apres_l_etl(
    commande, capsys
):
    """Quand l'ETL a tourné, ses contrôles de qualité puis ses lignes chargées s'affichent."""
    commande.valeur = {
        "etl": {"qualite": {"villes_nulles": 0}, "charges": {"villes": 7}}
    }
    commande("--etapes", "etl")
    assert capsys.readouterr().out == (
        "Contrôles de qualité : {'villes_nulles': 0}\nLignes chargées : {'villes': 7}\n"
    )
