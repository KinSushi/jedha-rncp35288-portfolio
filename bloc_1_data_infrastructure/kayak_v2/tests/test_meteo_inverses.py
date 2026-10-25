# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests INVERSES de la collecte météo : ce que le code refuse, ignore ou laisse intact.

Complète tests/test_meteo.py. Chaque test prouve une règle précise : un statut HTTP en erreur
est propagé et jamais converti en prévisions, une donnée absente ou invalide ne fabrique pas de
valeur, une ville déjà archivée n'est pas redemandée, une session fournie n'est pas modifiée,
une ville en échec est nommée dans l'erreur finale sans emporter les autres. Les quelques tests
directs vérifient les valeurs exactes (URL, paramètres, arrondis) que des mutants pourraient
altérer sans que rien ne casse.

Aucun appel réseau : la session HTTP et les fournisseurs sont factices, ``time.sleep`` est
remplacé par un enregistreur et la date de collecte est fixée.
"""

import copy
import json
import logging
import math
import re
from urllib.parse import quote

import pandas as pd
import pytest
import requests

from kayak import config
from kayak.collecte import meteo
from kayak.collecte.meteo import (
    SCHEMA_PREVISION,
    collecter_meteo,
    convertir_openweather,
    meteo_open_meteo,
    meteo_openweather,
)
from kayak.stockage.data_lake import DataLake

DATE = "2026-10-10"
DOSSIER = f"raw/meteo/date={DATE}"


class _Reponse:
    """Réponse HTTP minimale : statut, JSON, et compteur d'appels à ``json()``."""

    def __init__(self, statut=200, donnees=None, erreur_json=None):
        self.status_code = statut
        self.donnees = donnees if donnees is not None else {}
        self.erreur_json = erreur_json
        self.appels_json = 0

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} erreur simulée")

    def json(self):
        self.appels_json += 1
        if self.erreur_json is not None:
            raise self.erreur_json
        return self.donnees


class _Session:
    """Session factice : enregistre chaque appel à ``get`` et rend la réponse programmée."""

    def __init__(self, reponse=None, erreur=None):
        self.reponse = reponse if reponse is not None else _Reponse()
        self.erreur = erreur
        self.appels = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.appels.append({"url": url, "params": params, "timeout": timeout})
        if self.erreur is not None:
            raise self.erreur
        return self.reponse


@pytest.fixture(autouse=True)
def pauses(monkeypatch) -> list[float]:
    """Date fixée, pas de clé OpenWeather, pauses enregistrées plutôt que subies."""
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", DATE)
    monkeypatch.setattr(config, "OPENWEATHER_API_KEY", None)
    enregistrees: list[float] = []
    monkeypatch.setattr(meteo.time, "sleep", enregistrees.append)
    return enregistrees


def _prevision(valeur: float) -> dict:
    """Une prévision d'un jour au schéma commun, dont la température dépend de ``valeur``."""
    return {
        "date": "2026-10-11",
        "temp_max_c": valeur,
        "temp_min_c": valeur - 8,
        "pluie_mm": 0.0,
        "proba_pluie_pct": 5.0,
        "ensoleillement_h": 9.0,
        "vent_max_kmh": 12.0,
        "source": "open-meteo",
    }


class _Fournisseur:
    """Faux fournisseur de prévisions : mémorise ses arguments, échoue pour certaines latitudes."""

    def __init__(self, echecs=None):
        self.demandes: list[tuple] = []
        self.echecs = echecs or {}

    def __call__(self, *arguments):
        self.demandes.append(arguments)
        latitude = arguments[0]
        if latitude in self.echecs:
            raise self.echecs[latitude]
        return [_prevision(latitude)]


def _coordonnees(*villes) -> pd.DataFrame:
    """Tableau de coordonnées : un tuple (id_ville, ville, latitude, longitude) par ville."""
    return pd.DataFrame(villes, columns=["id_ville", "ville", "latitude", "longitude"])


def _lake(dossier) -> DataLake:
    return DataLake(bucket=None, racine_locale=dossier)


# --- meteo_open_meteo -------------------------------------------------------------------------


def _requete_open_meteo(lat: float, lon: float) -> dict:
    """La requête exacte attendue par Open-Meteo : URL, six variables, fuseau, 7 jours, délai."""
    return {
        "url": "https://api.open-meteo.com/v1/forecast",
        "params": {
            "latitude": lat,
            "longitude": lon,
            "daily": (
                "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                "precipitation_probability_max,sunshine_duration,wind_speed_10m_max"
            ),
            "timezone": "Europe/Paris",
            "forecast_days": 7,
        },
        "timeout": 30,
    }


def test_open_meteo_convertit_les_secondes_en_heures_et_laisse_le_payload_inchange():
    """Toutes les séries sont reprises champ par champ ; l'ensoleillement passe en heures.

    86400 s valent 24 h, 12345 s valent 3,43 h (arrondi à 2 décimales), 1000 s valent 0,28 h et
    0 s vaut 0,0 h : zéro est une vraie valeur, pas une donnée manquante.
    """
    payload = {
        "daily": {
            "time": ["2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14"],
            "temperature_2m_max": [18.5, 19.0, 17.2, 16.0],
            "temperature_2m_min": [9.1, 10.4, 8.8, 7.5],
            "precipitation_sum": [0.0, 2.3, 5.6, 0.1],
            "precipitation_probability_max": [5, 60, 85, 20],
            "sunshine_duration": [86400, 12345, 1000, 0],
            "wind_speed_10m_max": [12.0, 25.5, 40.1, 8.0],
        }
    }
    avant = copy.deepcopy(payload)
    session = _Session(_Reponse(donnees=payload))
    previsions = meteo_open_meteo(43.5, 7.25, session)
    assert session.appels == [_requete_open_meteo(43.5, 7.25)]
    assert payload == avant
    assert previsions == [
        {
            "date": "2026-10-11",
            "temp_max_c": 18.5,
            "temp_min_c": 9.1,
            "pluie_mm": 0.0,
            "proba_pluie_pct": 5,
            "ensoleillement_h": 24.0,
            "vent_max_kmh": 12.0,
            "source": "open-meteo",
        },
        {
            "date": "2026-10-12",
            "temp_max_c": 19.0,
            "temp_min_c": 10.4,
            "pluie_mm": 2.3,
            "proba_pluie_pct": 60,
            "ensoleillement_h": 3.43,
            "vent_max_kmh": 25.5,
            "source": "open-meteo",
        },
        {
            "date": "2026-10-13",
            "temp_max_c": 17.2,
            "temp_min_c": 8.8,
            "pluie_mm": 5.6,
            "proba_pluie_pct": 85,
            "ensoleillement_h": 0.28,
            "vent_max_kmh": 40.1,
            "source": "open-meteo",
        },
        {
            "date": "2026-10-14",
            "temp_max_c": 16.0,
            "temp_min_c": 7.5,
            "pluie_mm": 0.1,
            "proba_pluie_pct": 20,
            "ensoleillement_h": 0.0,
            "vent_max_kmh": 8.0,
            "source": "open-meteo",
        },
    ]


@pytest.mark.parametrize("statut", [400, 404, 429, 500, 503])
def test_open_meteo_propage_un_statut_http_en_erreur_sans_lire_le_json(statut):
    """Un refus du serveur (4xx, 5xx) est propagé : jamais converti en prévisions vides.

    Le corps de la réponse n'est même pas lu, puisqu'une page d'erreur n'est pas une prévision.
    Une seule requête, exactement celle d'Open-Meteo, est émise : pas de nouvel essai.
    """
    reponse = _Reponse(statut=statut, donnees={"daily": {"time": ["2026-10-11"]}})
    session = _Session(reponse)
    with pytest.raises(requests.HTTPError, match=f"{statut} erreur simulée"):
        meteo_open_meteo(43.5, 7.25, session)
    assert reponse.appels_json == 0
    assert session.appels == [_requete_open_meteo(43.5, 7.25)]


def test_open_meteo_propage_une_coupure_reseau():
    """Une coupure avant toute réponse remonte telle quelle à l'appelant, sans nouvel essai."""
    session = _Session(erreur=requests.ConnectionError("coupure réseau simulée"))
    with pytest.raises(requests.ConnectionError, match="coupure réseau simulée"):
        meteo_open_meteo(43.5, 7.25, session)
    assert session.appels == [_requete_open_meteo(43.5, 7.25)]


def test_open_meteo_propage_un_corps_qui_n_est_pas_du_json():
    """Un corps illisible lève ValueError : la collecte la traite ensuite comme un échec."""
    reponse = _Reponse(erreur_json=ValueError("corps illisible"))
    with pytest.raises(ValueError, match="corps illisible"):
        meteo_open_meteo(43.5, 7.25, _Session(reponse))


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"hourly": {"time": ["2026-10-11T00:00"]}},
        {"daily": {}},
        {"daily": {"temperature_2m_max": [10.0, 11.0]}},
    ],
)
def test_open_meteo_sans_dates_ne_fabrique_aucune_prevision_et_laisse_le_payload_inchange(payload):
    """Sans ``daily`` ni dates (``time``), aucune prévision n'est inventée.

    Des températures sans date ne sont pas exploitables : la liste est vide. Le payload reçu
    n'est pas modifié par la conversion.
    """
    avant = copy.deepcopy(payload)
    assert meteo_open_meteo(43.5, 7.25, _Session(_Reponse(donnees=payload))) == []
    assert payload == avant


def test_open_meteo_series_absentes_ou_vides_donnent_none_et_le_payload_reste_inchange():
    """Une série absente ou une valeur None donne None : aucune valeur n'est inventée.

    Cas 1 : seules les dates sont fournies. Cas 2 : les séries existent mais portent None
    (l'ensoleillement None ne doit pas être divisé par 3600).
    """
    seulement_dates = {"daily": {"time": ["2026-10-11", "2026-10-12"]}}
    avant = copy.deepcopy(seulement_dates)
    previsions = meteo_open_meteo(1.0, 2.0, _Session(_Reponse(donnees=seulement_dates)))
    assert previsions == [
        {
            "date": date,
            "temp_max_c": None,
            "temp_min_c": None,
            "pluie_mm": None,
            "proba_pluie_pct": None,
            "ensoleillement_h": None,
            "vent_max_kmh": None,
            "source": "open-meteo",
        }
        for date in ("2026-10-11", "2026-10-12")
    ]
    assert seulement_dates == avant

    series_none = {
        "daily": {
            "time": ["2026-10-11"],
            "temperature_2m_max": [None],
            "temperature_2m_min": [None],
            "precipitation_sum": [None],
            "precipitation_probability_max": [None],
            "sunshine_duration": [None],
            "wind_speed_10m_max": [None],
        }
    }
    assert meteo_open_meteo(1.0, 2.0, _Session(_Reponse(donnees=series_none))) == [
        {
            "date": "2026-10-11",
            "temp_max_c": None,
            "temp_min_c": None,
            "pluie_mm": None,
            "proba_pluie_pct": None,
            "ensoleillement_h": None,
            "vent_max_kmh": None,
            "source": "open-meteo",
        }
    ]


def test_open_meteo_serie_trop_courte_vide_ou_nulle_donne_none_et_le_payload_reste_inchange():
    """Une série plus courte que les dates, vide ou nulle ne fait pas échouer la ville.

    Les jours sans mesure valent None ; les jours mesurés gardent leur valeur (le dernier jour
    mesuré d'une série courte est bien lu). Le payload reçu n'est pas modifié.
    """
    payload = {
        "daily": {
            "time": ["2026-10-11", "2026-10-12", "2026-10-13"],
            "temperature_2m_max": [18.5],
            "temperature_2m_min": [1.0, 2.0],
            "precipitation_sum": None,
            "precipitation_probability_max": [10, 20, 30],
            "sunshine_duration": [7200],
            "wind_speed_10m_max": [],
        }
    }
    avant = copy.deepcopy(payload)
    previsions = meteo_open_meteo(1.0, 2.0, _Session(_Reponse(donnees=payload)))
    assert previsions == [
        {
            "date": "2026-10-11",
            "temp_max_c": 18.5,
            "temp_min_c": 1.0,
            "pluie_mm": None,
            "proba_pluie_pct": 10,
            "ensoleillement_h": 2.0,
            "vent_max_kmh": None,
            "source": "open-meteo",
        },
        {
            "date": "2026-10-12",
            "temp_max_c": None,
            "temp_min_c": 2.0,
            "pluie_mm": None,
            "proba_pluie_pct": 20,
            "ensoleillement_h": None,
            "vent_max_kmh": None,
            "source": "open-meteo",
        },
        {
            "date": "2026-10-13",
            "temp_max_c": None,
            "temp_min_c": None,
            "pluie_mm": None,
            "proba_pluie_pct": 30,
            "ensoleillement_h": None,
            "vent_max_kmh": None,
            "source": "open-meteo",
        },
    ]
    assert payload == avant


# --- convertir_openweather --------------------------------------------------------------------


def test_convertir_openweather_arrondit_et_convertit_les_unites_sans_toucher_l_entree():
    """Probabilité en pourcentage (1 décimale), vent de m/s en km/h (x 3,6, 2 décimales).

    0,3333 donne 33,3 % ; 0,3456 donne 34,6 % ; 0,5 donne 50,0 %. 5,123 m/s donnent 18,44 km/h.
    Chaque jour porte exactement les champs du schéma commun, sans « id_ville ».
    """
    reponse = {
        "daily": [
            {"dt": 1704067200, "temp": {"max": 12.5, "min": 3.2}, "pop": 0.3333,
             "wind_speed": 5.123},
            {"dt": 1704153600, "temp": {"max": 14.0, "min": 4.0}, "pop": 0.3456,
             "wind_speed": 10},
            {"dt": 1704240000, "temp": {"max": 9.0, "min": 1.0}, "pop": 0.5,
             "wind_speed": 2.77},
        ]
    }
    avant = copy.deepcopy(reponse)
    previsions = convertir_openweather(reponse)
    assert reponse == avant
    assert [p["proba_pluie_pct"] for p in previsions] == [33.3, 34.6, 50.0]
    assert [p["vent_max_kmh"] for p in previsions] == [18.44, 36.0, 9.97]
    assert [p["date"] for p in previsions] == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert [p["temp_max_c"] for p in previsions] == [12.5, 14.0, 9.0]
    assert [p["temp_min_c"] for p in previsions] == [3.2, 4.0, 1.0]
    champs_attendus = [colonne for colonne in SCHEMA_PREVISION if colonne != "id_ville"]
    assert all(list(prevision) == champs_attendus for prevision in previsions)
    assert {p["source"] for p in previsions} == {"openweather"}
    assert {p["ensoleillement_h"] for p in previsions} == {None}


@pytest.mark.parametrize("reponse", [{}, {"current": {}}, {"daily": []}])
def test_convertir_openweather_sans_jour_donne_une_liste_vide_et_l_entree_reste_inchangee(reponse):
    """Réponse vide, sans ``daily`` ou avec ``daily`` vide : aucune prévision, entrée intacte."""
    avant = copy.deepcopy(reponse)
    assert convertir_openweather(reponse) == []
    assert reponse == avant


def test_convertir_openweather_dt_absent_donne_une_date_none_et_l_entree_reste_inchangee():
    """Sans horodatage ``dt``, la date est None : aucune date n'est inventée."""
    reponse = {"daily": [{"temp": {"max": 12.5, "min": 3.2}, "pop": 0.5, "wind_speed": 1}]}
    avant = copy.deepcopy(reponse)
    assert [p["date"] for p in convertir_openweather(reponse)] == [None]
    assert reponse == avant


def test_convertir_openweather_dt_zero_est_une_vraie_date_et_l_entree_reste_inchangee():
    """Le 0 Unix est le 1er janvier 1970 UTC : un test « if dt » le prendrait à tort pour None."""
    reponse = {"daily": [{"dt": 0}]}
    avant = copy.deepcopy(reponse)
    assert [p["date"] for p in convertir_openweather(reponse)] == ["1970-01-01"]
    assert reponse == avant


def test_convertir_openweather_date_en_utc_aux_bornes_de_minuit_entree_inchangee():
    """La date est celle du jour UTC, quel que soit le fuseau de la machine.

    23:59:59 UTC reste le 1er janvier ; une seconde plus tard, c'est le 2.
    """
    reponse = {"daily": [{"dt": 1704153599}, {"dt": 1704153600}]}
    avant = copy.deepcopy(reponse)
    assert [p["date"] for p in convertir_openweather(reponse)] == ["2024-01-01", "2024-01-02"]
    assert reponse == avant


def test_convertir_openweather_refuse_un_horodatage_textuel():
    """Un ``dt`` en texte lève TypeError : un horodatage mal typé n'est pas deviné."""
    with pytest.raises(TypeError, match="str"):
        convertir_openweather({"daily": [{"dt": "1704067200"}]})


def test_convertir_openweather_pluie_non_numerique_vaut_zero_et_l_entree_reste_inchangee():
    """Seule une pluie numérique est retenue ; dict (cumul horaire), texte, None ou absence : 0.

    Les nombres (entier ou décimal, y compris 0) sont repris tels quels.
    """
    pluies = [{"1h": 3.2}, "2.5", None, 3, 0.4, 0]
    reponse = {"daily": [{"dt": 1704067200, "rain": pluie} for pluie in pluies]}
    reponse["daily"].append({"dt": 1704067200})  # clé « rain » absente
    avant = copy.deepcopy(reponse)
    assert [p["pluie_mm"] for p in convertir_openweather(reponse)] == [0, 0, 0, 3, 0.4, 0, 0]
    assert reponse == avant


def test_convertir_openweather_pop_et_vent_none_donnent_none_et_absents_donnent_zero():
    """Une valeur explicitement nulle reste None ; une clé absente vaut 0 (ni 1, ni None)."""
    reponse = {
        "daily": [
            {"dt": 1704067200, "pop": None, "wind_speed": None},
            {"dt": 1704067200},
        ]
    }
    avant = copy.deepcopy(reponse)
    previsions = convertir_openweather(reponse)
    assert [p["proba_pluie_pct"] for p in previsions] == [None, 0]
    assert [p["vent_max_kmh"] for p in previsions] == [None, 0]
    assert reponse == avant


def test_convertir_openweather_temperatures_absentes_donnent_none_et_l_entree_reste_inchangee():
    """Sans bloc ``temp`` ou sans ``max``/``min``, les températures sont None, pas une erreur."""
    reponse = {"daily": [{"dt": 1704067200}, {"dt": 1704067200, "temp": {"max": 7.0}}]}
    avant = copy.deepcopy(reponse)
    previsions = convertir_openweather(reponse)
    assert reponse == avant
    assert [p["temp_max_c"] for p in previsions] == [None, 7.0]
    assert [p["temp_min_c"] for p in previsions] == [None, None]


# --- meteo_openweather ------------------------------------------------------------------------


def test_openweather_interroge_l_url_officielle_avec_les_parametres_exacts():
    """La requête One Call 3.0 exclut les blocs inutiles, en unités métriques, avec la clé."""
    reponse = _Reponse(donnees={"daily": [{"dt": 1704067200, "temp": {"max": 1.0, "min": 0.0}}]})
    session = _Session(reponse)
    previsions = meteo_openweather(43.5, 7.25, "cle-de-test", session)
    assert session.appels == [
        {
            "url": "https://api.openweathermap.org/data/3.0/onecall",
            "params": {
                "lat": 43.5,
                "lon": 7.25,
                "exclude": "current,minutely,hourly,alerts",
                "units": "metric",
                "appid": "cle-de-test",
            },
            "timeout": 30,
        }
    ]
    assert [p["date"] for p in previsions] == ["2024-01-01"]
    assert {p["source"] for p in previsions} == {"openweather"}


@pytest.mark.parametrize("statut", [401, 404, 429, 500])
def test_openweather_propage_un_statut_http_en_erreur_sans_lire_le_json(statut):
    """Clé refusée (401), quota (429), erreur serveur : l'erreur remonte, aucune conversion.

    Une seule requête est émise : pas de nouvel essai avec une clé déjà refusée.
    """
    reponse = _Reponse(statut=statut, donnees={"daily": [{"dt": 1704067200}]})
    session = _Session(reponse)
    with pytest.raises(requests.HTTPError, match=f"{statut} erreur simulée"):
        meteo_openweather(43.5, 7.25, "cle-de-test", session)
    assert reponse.appels_json == 0
    assert [appel["url"] for appel in session.appels] == [
        "https://api.openweathermap.org/data/3.0/onecall"
    ]


def test_openweather_propage_un_delai_depasse():
    """Un dépassement de délai remonte à l'appelant au lieu de rendre des prévisions vides."""
    session = _Session(erreur=requests.Timeout("délai dépassé"))
    with pytest.raises(requests.Timeout, match="délai dépassé"):
        meteo_openweather(43.5, 7.25, "cle-de-test", session)


# --- collecter_meteo : choix du fournisseur et session ----------------------------------------


def test_cle_openweather_presente_interroge_openweather_et_laisse_open_meteo_intact(
    tmp_path, monkeypatch
):
    """Avec une clé, OpenWeather reçoit (lat, lon, clé, session) ; Open-Meteo n'est pas appelé."""
    monkeypatch.setattr(config, "OPENWEATHER_API_KEY", "cle-de-test")
    openweather, open_meteo = _Fournisseur(), _Fournisseur()
    monkeypatch.setattr(meteo, "meteo_openweather", openweather)
    monkeypatch.setattr(meteo, "meteo_open_meteo", open_meteo)
    session = _Session()
    lake = _lake(tmp_path)
    collecter_meteo(_coordonnees((1, "Nice", 43.7, 7.27)), lake, session=session, pause_s=0)
    assert openweather.demandes == [(43.7, 7.27, "cle-de-test", session)]
    assert open_meteo.demandes == []
    archive = json.loads(lake.lire(f"{DOSSIER}/ville_01.json"))
    assert archive["source"] == "openweather"


@pytest.mark.parametrize("cle", [None, ""])
def test_sans_cle_ou_cle_vide_interroge_open_meteo_et_laisse_openweather_intact(
    tmp_path, monkeypatch, cle
):
    """Clé absente ou vide : pas d'OpenWeather ; Open-Meteo reçoit (lat, lon, session)."""
    monkeypatch.setattr(config, "OPENWEATHER_API_KEY", cle)
    openweather, open_meteo = _Fournisseur(), _Fournisseur()
    monkeypatch.setattr(meteo, "meteo_openweather", openweather)
    monkeypatch.setattr(meteo, "meteo_open_meteo", open_meteo)
    session = _Session()
    lake = _lake(tmp_path)
    collecter_meteo(_coordonnees((1, "Nice", 43.7, 7.27)), lake, session=session, pause_s=0)
    assert open_meteo.demandes == [(43.7, 7.27, session)]
    assert openweather.demandes == []
    assert json.loads(lake.lire(f"{DOSSIER}/ville_01.json"))["source"] == "open-meteo"


def test_session_par_defaut_porte_le_user_agent_et_une_session_fournie_reste_intacte(
    tmp_path, monkeypatch
):
    """Sans session, le module en crée une qui s'identifie ; une session fournie n'est pas touchée.

    Le robot déclare son User-Agent (identification honnête). Une session passée par l'appelant
    est utilisée telle quelle : ses en-têtes restent inchangés et aucune autre n'est créée.
    """
    creees = []

    class _SessionCreee:
        def __init__(self):
            self.headers = {}
            creees.append(self)

    monkeypatch.setattr(requests, "Session", _SessionCreee)
    fournisseur = _Fournisseur()
    monkeypatch.setattr(meteo, "meteo_open_meteo", fournisseur)
    coordonnees = _coordonnees((1, "Nice", 43.7, 7.27))
    collecter_meteo(coordonnees, _lake(tmp_path / "defaut"), pause_s=0)
    assert creees[0].headers == {"User-Agent": config.USER_AGENT}
    assert fournisseur.demandes[0][-1] is creees[0]

    fournie = _Session()
    fournie.headers = {"X-Test": "1"}
    avant = dict(fournie.headers)
    nombre_avant = len(creees)
    collecter_meteo(coordonnees, _lake(tmp_path / "fournie"), session=fournie, pause_s=0)
    assert fournie.headers == avant
    assert len(creees) == nombre_avant
    assert fournisseur.demandes[1][-1] is fournie


# --- collecter_meteo : archives, pauses, coordonnées ------------------------------------------


def test_collecte_nominale_archive_chaque_ville_et_rend_le_tableau_au_schema(tmp_path, monkeypatch):
    """Une archive zéro-complétée par ville, le consolidé en JSON Lines, le tableau du schéma.

    L'identifiant 7 s'écrit ``ville_07`` et le 123 ``ville_123``. Les accents sont conservés
    tels quels (UTF-8, pas de séquences \\u) dans l'archive comme dans le consolidé.
    """
    fournisseur = _Fournisseur()
    monkeypatch.setattr(meteo, "meteo_open_meteo", fournisseur)
    lake = _lake(tmp_path)
    coordonnees = _coordonnees((7, "Besançon", 47.2, 6.0), (123, "Nîmes", 43.8, 4.3))
    resultat = collecter_meteo(coordonnees, lake, pause_s=0)

    assert lake.existe(f"{DOSSIER}/ville_07.json")
    assert lake.existe(f"{DOSSIER}/ville_123.json")
    assert not lake.existe(f"{DOSSIER}/ville_7.json")
    brut = lake.lire(f"{DOSSIER}/ville_07.json")
    assert "Besançon".encode() in brut
    assert b"\\u00e7" not in brut
    consolide = lake.lire(f"{DOSSIER}/previsions.jsonl")
    assert "Besançon".encode() in consolide
    assert b"\\u00e7" not in consolide
    assert consolide.endswith(b"}\n")
    lignes = [json.loads(ligne) for ligne in consolide.decode("utf-8").splitlines()]
    assert [(ligne["id_ville"], ligne["ville"], ligne["source"]) for ligne in lignes] == [
        (7, "Besançon", "open-meteo"),
        (123, "Nîmes", "open-meteo"),
    ]
    assert resultat.columns.tolist() == SCHEMA_PREVISION
    assert resultat["id_ville"].tolist() == [7, 123]
    assert resultat["temp_max_c"].tolist() == [47.2, 43.8]
    assert resultat["temp_min_c"].tolist() == [39.2, 35.8]


def test_ville_deja_archivee_n_est_pas_redemandee_et_son_archive_reste_inchangee(
    tmp_path, monkeypatch, pauses
):
    """Une archive présente est relue : aucun appel au fournisseur, aucune pause, aucun écrasement.

    Les prévisions du tableau viennent de l'archive, pas du fournisseur.
    """
    fournisseur = _Fournisseur()
    monkeypatch.setattr(meteo, "meteo_open_meteo", fournisseur)
    lake = _lake(tmp_path)
    archive = {
        "id_ville": 1,
        "ville": "Nice",
        "previsions": [_prevision(99.0)],
        "source": "archive-de-test",
    }
    lake.ecrire(f"{DOSSIER}/ville_01.json", json.dumps(archive))
    contenu_avant = lake.lire(f"{DOSSIER}/ville_01.json")
    resultat = collecter_meteo(_coordonnees((1, "Nice", 43.7, 7.27)), lake, pause_s=2.5)
    assert fournisseur.demandes == []
    assert pauses == []
    assert lake.lire(f"{DOSSIER}/ville_01.json") == contenu_avant
    assert resultat["temp_max_c"].tolist() == [99.0]


def test_archive_illisible_leve_une_erreur_au_lieu_d_etre_ignoree(tmp_path, monkeypatch):
    """Une archive de ville tronquée n'est ni écrasée ni passée sous silence : l'erreur remonte.

    Le consolidé n'est pas écrit et le fournisseur n'est pas interrogé.
    """
    fournisseur = _Fournisseur()
    monkeypatch.setattr(meteo, "meteo_open_meteo", fournisseur)
    lake = _lake(tmp_path)
    lake.ecrire(f"{DOSSIER}/ville_01.json", '{"id_ville": 1, "ville": "Nice", "prev')
    with pytest.raises(json.JSONDecodeError):
        collecter_meteo(_coordonnees((1, "Nice", 43.7, 7.27)), lake, pause_s=0)
    assert fournisseur.demandes == []
    assert not lake.existe(f"{DOSSIER}/previsions.jsonl")


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(math.nan, 7.0), (43.0, math.nan), (None, None), (None, 7.0)],
)
def test_coordonnees_manquantes_n_appellent_aucun_fournisseur_et_le_tableau_reste_inchange(
    tmp_path, monkeypatch, latitude, longitude
):
    """Une latitude OU une longitude absente (NaN ou None) suffit à ne pas interroger l'API.

    Le tableau de coordonnées de l'appelant n'est pas modifié. La ville est archivée avec la
    source « aucune » et aucune prévision, et reste absente du tableau rendu.
    """
    fournisseur = _Fournisseur()
    monkeypatch.setattr(meteo, "meteo_open_meteo", fournisseur)
    lake = _lake(tmp_path)
    coordonnees = _coordonnees((5, "Inconnue", latitude, longitude))
    avant = coordonnees.copy(deep=True)
    resultat = collecter_meteo(coordonnees, lake, pause_s=0)
    pd.testing.assert_frame_equal(coordonnees, avant)
    assert fournisseur.demandes == []
    archive = json.loads(lake.lire(f"{DOSSIER}/ville_05.json"))
    assert archive == {"id_ville": 5, "ville": "Inconnue", "previsions": [], "source": "aucune"}
    assert resultat.empty
    assert resultat.columns.tolist() == SCHEMA_PREVISION


def test_aucune_ville_n_appelle_aucun_fournisseur_et_laisse_le_tableau_inchange(
    tmp_path, monkeypatch, pauses
):
    """Sans ville, rien n'est demandé ni attendu ; le consolidé existe mais est vide.

    Le tableau rendu garde les colonnes du schéma ; le tableau reçu n'est pas modifié.
    """
    fournisseur = _Fournisseur()
    monkeypatch.setattr(meteo, "meteo_open_meteo", fournisseur)
    lake = _lake(tmp_path)
    coordonnees = _coordonnees()
    avant = coordonnees.copy(deep=True)
    resultat = collecter_meteo(coordonnees, lake, pause_s=2.5)
    pd.testing.assert_frame_equal(coordonnees, avant)
    assert fournisseur.demandes == []
    assert pauses == []
    assert lake.lire(f"{DOSSIER}/previsions.jsonl") == b""
    assert resultat.columns.tolist() == SCHEMA_PREVISION
    assert resultat.empty


def test_pause_suit_chaque_ville_interrogee_et_une_ville_archivee_reste_inchangee_sans_pause(
    tmp_path, monkeypatch, pauses
):
    """La pause injectée suit chaque ville interrogée ; une ville déjà archivée en est exempte.

    Sans cela, une relance de reprise attendrait inutilement devant le fournisseur. Sans
    ``pause_s``, la pause par défaut est d'une demi-seconde. L'archive existante n'est pas
    réécrite.
    """
    monkeypatch.setattr(meteo, "meteo_open_meteo", _Fournisseur())
    lake = _lake(tmp_path)
    archive = {"id_ville": 2, "ville": "B", "previsions": [], "source": "archive-de-test"}
    lake.ecrire(f"{DOSSIER}/ville_02.json", json.dumps(archive))
    contenu_avant = lake.lire(f"{DOSSIER}/ville_02.json")
    coordonnees = _coordonnees((1, "A", 10.0, 1.0), (2, "B", 20.0, 2.0), (3, "C", 30.0, 3.0))
    collecter_meteo(coordonnees, lake, pause_s=2.5)
    assert pauses == [2.5, 2.5]
    assert lake.lire(f"{DOSSIER}/ville_02.json") == contenu_avant
    pauses.clear()
    collecter_meteo(_coordonnees((9, "Z", 50.0, 5.0)), lake)
    assert pauses == [0.5]


def test_collecte_d_une_autre_date_laisse_inchangee_l_archive_de_la_date_du_jour(
    tmp_path, monkeypatch
):
    """Les fichiers vont sous la date de collecte demandée ; l'autre date n'est pas touchée."""
    monkeypatch.setattr(meteo, "meteo_open_meteo", _Fournisseur())
    lake = _lake(tmp_path)
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2027-01-02")
    collecter_meteo(_coordonnees((1, "A", 10.0, 1.0)), lake, pause_s=0)
    assert sorted(lake.lister("raw/meteo/date=2027-01-02/")) == [
        "raw/meteo/date=2027-01-02/previsions.jsonl",
        "raw/meteo/date=2027-01-02/ville_01.json",
    ]
    assert lake.lister(f"{DOSSIER}/") == []


# --- collecter_meteo : échecs -----------------------------------------------------------------


def test_echec_de_deux_villes_leve_une_erreur_qui_cite_chaque_ville_et_son_erreur(
    tmp_path, monkeypatch
):
    """Le message final nomme chaque ville manquante avec le type et le texte de son erreur.

    Il annonce aussi la reprise : une relance ne redemande que ces villes.
    """
    monkeypatch.setattr(
        meteo,
        "meteo_open_meteo",
        _Fournisseur(
            echecs={
                10.0: requests.ConnectionError("coupure A"),
                30.0: requests.Timeout("délai C"),
            }
        ),
    )
    coordonnees = _coordonnees((1, "A", 10.0, 1.0), (2, "B", 20.0, 2.0), (3, "C", 30.0, 3.0))
    attendu = (
        "prévisions manquantes pour 2 ville(s) : "
        "{'A': 'ConnectionError: coupure A', 'C': 'Timeout: délai C'} ; "
        "relancer l'étape reprend uniquement ces villes"
    )
    with pytest.raises(RuntimeError, match=re.escape(attendu)):
        collecter_meteo(coordonnees, _lake(tmp_path), pause_s=0)


def test_ville_en_echec_n_est_pas_archivee_mais_le_consolide_garde_les_autres(
    tmp_path, monkeypatch
):
    """Le consolidé est écrit même en cas d'échec ; la ville fautive n'a aucune archive.

    Sans archive, une relance la redemandera au lieu de la croire déjà traitée.
    """
    monkeypatch.setattr(
        meteo,
        "meteo_open_meteo",
        _Fournisseur(echecs={20.0: requests.ConnectionError("coupure")}),
    )
    lake = _lake(tmp_path)
    coordonnees = _coordonnees((1, "A", 10.0, 1.0), (2, "B", 20.0, 2.0), (3, "C", 30.0, 3.0))
    with pytest.raises(RuntimeError, match="'B': 'ConnectionError: coupure'"):
        collecter_meteo(coordonnees, lake, pause_s=0)
    assert not lake.existe(f"{DOSSIER}/ville_02.json")
    lignes = lake.lire(f"{DOSSIER}/previsions.jsonl").decode("utf-8").splitlines()
    assert [(json.loads(ligne)["id_ville"], json.loads(ligne)["ville"]) for ligne in lignes] == [
        (1, "A"),
        (3, "C"),
    ]


@pytest.mark.parametrize(
    ("erreur", "texte"),
    [
        (ValueError("JSON illisible"), "ValueError: JSON illisible"),
        (requests.HTTPError("503 saturé"), "HTTPError: 503 saturé"),
        (requests.Timeout("délai dépassé"), "Timeout: délai dépassé"),
    ],
)
def test_erreur_http_delai_ou_json_du_fournisseur_est_capturee_comme_un_echec_de_ville(
    tmp_path, monkeypatch, erreur, texte
):
    """RequestException et ValueError sont rattrapées : la ville est notée, les autres suivent."""
    monkeypatch.setattr(meteo, "meteo_open_meteo", _Fournisseur(echecs={10.0: erreur}))
    lake = _lake(tmp_path)
    coordonnees = _coordonnees((1, "A", 10.0, 1.0), (2, "B", 20.0, 2.0))
    with pytest.raises(RuntimeError, match=re.escape(f"{{'A': '{texte}'}}")):
        collecter_meteo(coordonnees, lake, pause_s=0)
    assert lake.existe(f"{DOSSIER}/ville_02.json")
    assert not lake.existe(f"{DOSSIER}/ville_01.json")


def test_erreur_de_programmation_n_est_pas_deguisee_en_echec_de_ville(tmp_path, monkeypatch):
    """Une TypeError du fournisseur n'est pas une panne réseau : elle remonte telle quelle.

    Le consolidé n'est alors pas écrit, mais la ville déjà obtenue reste archivée.
    """
    monkeypatch.setattr(
        meteo, "meteo_open_meteo", _Fournisseur(echecs={20.0: TypeError("bogue interne")})
    )
    lake = _lake(tmp_path)
    coordonnees = _coordonnees((1, "A", 10.0, 1.0), (2, "B", 20.0, 2.0))
    with pytest.raises(TypeError, match="bogue interne"):
        collecter_meteo(coordonnees, lake, pause_s=0)
    assert lake.existe(f"{DOSSIER}/ville_01.json")
    assert not lake.existe(f"{DOSSIER}/previsions.jsonl")


def test_echec_de_ville_journalise_un_avertissement_nommant_la_ville_et_l_erreur(
    tmp_path, monkeypatch, caplog
):
    """Le journal garde la trace de la ville en échec, au niveau avertissement."""
    monkeypatch.setattr(
        meteo,
        "meteo_open_meteo",
        _Fournisseur(echecs={10.0: requests.ConnectionError("coupure réseau simulée")}),
    )
    caplog.set_level(logging.WARNING)
    with pytest.raises(RuntimeError, match="Nice"):
        collecter_meteo(_coordonnees((1, "Nice", 10.0, 1.0)), _lake(tmp_path), pause_s=0)
    enregistrements = [
        (e.name, e.levelno, e.getMessage()) for e in caplog.records if e.name == meteo.__name__
    ]
    assert enregistrements == [
        (
            "kayak.collecte.meteo",
            logging.WARNING,
            "Nice : prévisions non obtenues (ConnectionError: coupure réseau simulée)",
        )
    ]


CLES_A_MASQUER = ["CLE-SECRETE-0123456789", "cle/secrète+01=&x"]


def _refus_401(cle: str) -> requests.Response:
    """Vraie réponse 401 de requests : son erreur cite l'URL, clé encodée en pourcentage."""
    refus = requests.Response()
    refus.status_code = 401
    refus.reason = "Unauthorized"
    refus.url = (
        "https://api.openweathermap.org/data/3.0/onecall?lat=10.0&lon=1.0"
        f"&units=metric&appid={quote(cle, safe='')}"
    )
    return refus


@pytest.mark.parametrize("cle", CLES_A_MASQUER)
def test_cle_openweather_est_masquee_en_forme_encodee_dans_le_journal_et_l_erreur_finale(
    tmp_path, monkeypatch, caplog, cle
):
    """L'erreur HTTP de requests cite l'URL, donc la clé : elle est remplacée par « *** ».

    La clé n'apparaît ni dans le journal ni dans le message de l'erreur finale, ni sous sa forme
    brute ni sous sa forme encodée en pourcentage (celle de l'URL).
    """
    monkeypatch.setattr(config, "OPENWEATHER_API_KEY", cle)
    caplog.set_level(logging.WARNING)
    session = _Session(_refus_401(cle))
    with pytest.raises(RuntimeError) as erreur:
        collecter_meteo(_coordonnees((1, "Nice", 10.0, 1.0)), _lake(tmp_path), session, pause_s=0)
    message_masque = (
        "HTTPError: 401 Client Error: Unauthorized for url: "
        "https://api.openweathermap.org/data/3.0/onecall?lat=10.0&lon=1.0&units=metric&appid=***"
    )
    assert f"{{'Nice': '{message_masque}'}}" in str(erreur.value)
    assert "***" in caplog.text
    for secret in (cle, quote(cle, safe="")):
        assert secret not in str(erreur.value)
        assert secret not in caplog.text
    assert [r.getMessage() for r in caplog.records] == [
        f"Nice : prévisions non obtenues ({message_masque})"
    ]


@pytest.mark.parametrize("cle", CLES_A_MASQUER)
def test_cle_openweather_est_masquee_en_forme_brute_et_a_chaque_occurrence(
    tmp_path, monkeypatch, caplog, cle
):
    """Toutes les occurrences de la clé brute d'un message d'erreur sont remplacées par « *** »."""
    monkeypatch.setattr(config, "OPENWEATHER_API_KEY", cle)
    caplog.set_level(logging.WARNING)
    session = _Session(erreur=requests.ConnectionError(f"échec appid={cle} puis appid={cle}"))
    with pytest.raises(RuntimeError) as erreur:
        collecter_meteo(_coordonnees((1, "Nice", 10.0, 1.0)), _lake(tmp_path), session, pause_s=0)
    assert "{'Nice': 'ConnectionError: échec appid=*** puis appid=***'}" in str(erreur.value)
    assert cle not in str(erreur.value)
    assert cle not in caplog.text
    assert "***" in caplog.text


@pytest.mark.parametrize("cle", [None, ""])
def test_sans_cle_le_message_d_erreur_n_est_pas_masque_et_reste_inchange(
    tmp_path, monkeypatch, cle
):
    """Sans clé, rien à masquer : le texte de l'erreur est repris tel quel (aucun « *** »).

    Une clé vide ne doit pas insérer « *** » entre chaque caractère du message.
    """
    monkeypatch.setattr(config, "OPENWEATHER_API_KEY", cle)
    monkeypatch.setattr(
        meteo,
        "meteo_open_meteo",
        _Fournisseur(echecs={10.0: requests.ConnectionError("coupure réseau")}),
    )
    with pytest.raises(RuntimeError) as erreur:
        collecter_meteo(_coordonnees((1, "Nice", 10.0, 1.0)), _lake(tmp_path), pause_s=0)
    assert "{'Nice': 'ConnectionError: coupure réseau'}" in str(erreur.value)
    assert "***" not in str(erreur.value)


def test_message_d_erreur_sans_la_cle_reste_inchange_meme_avec_une_cle_configuree(
    tmp_path, monkeypatch
):
    """Avec une clé configurée, un message qui ne la contient pas n'est pas altéré."""
    monkeypatch.setattr(config, "OPENWEATHER_API_KEY", "CLE-SECRETE-0123456789")
    session = _Session(erreur=requests.Timeout("délai dépassé"))
    with pytest.raises(RuntimeError) as erreur:
        collecter_meteo(_coordonnees((1, "Nice", 10.0, 1.0)), _lake(tmp_path), session, pause_s=0)
    assert "{'Nice': 'Timeout: délai dépassé'}" in str(erreur.value)
