# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Collecte des prévisions météo sur 7 jours.

Le module supporte deux fournisseurs : OpenWeather (One Call 3.0, clé
requise) et Open-Meteo (gratuit, sans clé). Les données brutes sont
archivées au format JSON Lines pour faciliter la relecture par Spark.

CHOIX
-----
- **Retenu : deux fournisseurs derrière un même schéma** (``SCHEMA_PREVISION``). OpenWeather est
  utilisé si une clé est configurée, sinon Open-Meteo, qui est gratuit et sans clé : le projet
  tourne sans compte. *Écarté* : un seul fournisseur, qui lierait le projet à une clé.
- **Retenu : archiver la réponse de CHAQUE ville dès sa réception** (``ville_NN.json``), puis un
  consolidé JSON Lines pour Spark. *Écarté* : tout écrire à la fin, car une panne ou un quota
  atteint en cours de route ferait redemander les villes déjà obtenues.
- **Retenu : une ville en échec n'emporte pas les autres**, et l'étape lève UNE erreur finale qui
  nomme les villes manquantes. *Écarté* : s'arrêter à la première erreur, ou taire les échecs et
  laisser l'ETL classer des destinations sans météo.
- **Retenu : masquer la clé d'API dans les messages d'erreur.** *Écarté* : relayer l'erreur de
  ``requests`` telle quelle, dont le texte contient l'URL complète et donc la clé.

OÙ
---
- **Place** : étape ``meteo`` de la collecte (kayak/pipeline.py:151), après le géocodage ; son
  fichier ``previsions.jsonl`` est lu par l'ETL (kayak/etl/spark_etl.py:278).
- **Appelants** : ``kayak/pipeline.py``. **Dépendances** : ``requests``, pandas, ``kayak.config``
  (clé OpenWeather, User-Agent, date de collecte), ``kayak.stockage.data_lake``.

COMMENT
-------
- ``meteo_open_meteo`` et ``meteo_openweather`` interrogent un fournisseur et rendent la liste des
  jours au schéma commun ; ``convertir_openweather`` convertit unités et date ;
  ``collecter_meteo`` parcourt les villes, archive, consolide et signale les échecs.
- **Preuves** : l'URL, les paramètres d'Open-Meteo et la conversion des secondes en heures
  (tests/test_meteo_inverses.py:146) ; la conversion OpenWeather (tests/test_meteo.py:23) ; la
  reprise après un échec (tests/test_meteo.py:118).
- **Refus** (tests inverses) : une série trop courte donne ``None`` au lieu de faire échouer la
  ville (tests/test_meteo_inverses.py:311) ; la clé d'API n'apparaît ni dans le journal ni dans
  l'erreur (tests/test_meteo_inverses.py:915) ; une ville déjà archivée n'est pas redemandée
  (tests/test_meteo_inverses.py:631).

POURQUOI
--------
- Variables quotidiennes, durée d'ensoleillement en secondes (d'où la conversion en heures) et 7
  jours par défaut : documentation d'Open-Meteo, https://open-meteo.com/en/docs
  (lecture non consignée, à revérifier).
- Format de la réponse One Call 3.0 d'OpenWeather (``daily[].dt``, ``pop``, vitesse du vent en
  m/s) : documentation d'OpenWeather (non rouverte dans ce travail ; le code est
  testé sur des réponses fabriquées).
- Pourquoi l'archive par ville : voir la reprise testée dans tests/test_meteo.py:118.
- Pourquoi masquer la clé : défaut relevé par un test et corrigé lors de la mise en conformité ;
  code dans kayak/collecte/meteo.py:267.

JOURNALISATION
--------------
- Logger ``kayak.collecte.meteo``. Une ville en échec : un message de niveau WARNING « <ville> :
  prévisions non obtenues (<erreur>) » (kayak/collecte/meteo.py:269), où la clé d'API est masquée
  par ``***``.
- Aucun message INFO ni DEBUG. L'erreur finale (``RuntimeError``) reprend les échecs par ville.
- Les requêtes HTTP elles-mêmes sont consignées par ``kayak/collecte/journal.py``, pas par ce
  module.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests

from kayak import config
from kayak.stockage.data_lake import DataLake

journal = logging.getLogger(__name__)

SCHEMA_PREVISION = [
    "id_ville",
    "date",
    "temp_max_c",
    "temp_min_c",
    "pluie_mm",
    "proba_pluie_pct",
    "ensoleillement_h",
    "vent_max_kmh",
    "source",
]


def _valeur_du_jour(daily: dict[str, Any], cle: str, i: int) -> Any:
    """Valeur du jour ``i`` dans la série ``cle`` ; None si la série est absente ou trop courte.

    Un fournisseur qui tronque une série ne doit pas faire échouer toute la ville : le jour sans
    mesure garde la valeur « inconnue » (None), comme une série absente.
    """
    serie = daily.get(cle) or []
    return serie[i] if i < len(serie) else None


def meteo_open_meteo(lat: float, lon: float, session: requests.Session) -> list[dict[str, Any]]:
    """Récupère 7 jours de prévisions via Open-Meteo.

    L'ensoleillement est fourni en secondes ; il est converti en heures.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params: dict[str, str | float] = {
        "latitude": lat,
        "longitude": lon,
        # Variables quotidiennes séparées par des virgules (forme documentée par Open-Meteo).
        "daily": ",".join(
            [
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "precipitation_probability_max",
                "sunshine_duration",
                "wind_speed_10m_max",
            ]
        ),
        "timezone": "Europe/Paris",
        "forecast_days": 7,
    }
    reponse = session.get(url, params=params, timeout=30)
    reponse.raise_for_status()
    payload = reponse.json()

    daily = payload.get("daily", {})
    dates = daily.get("time", [])
    nb = len(dates)
    resultats = []
    for i in range(nb):
        sunshine_s = _valeur_du_jour(daily, "sunshine_duration", i)
        ensoleillement_h = round(sunshine_s / 3600, 2) if sunshine_s is not None else None
        resultats.append(
            {
                "date": dates[i],
                "temp_max_c": _valeur_du_jour(daily, "temperature_2m_max", i),
                "temp_min_c": _valeur_du_jour(daily, "temperature_2m_min", i),
                "pluie_mm": _valeur_du_jour(daily, "precipitation_sum", i),
                "proba_pluie_pct": _valeur_du_jour(daily, "precipitation_probability_max", i),
                "ensoleillement_h": ensoleillement_h,
                "vent_max_kmh": _valeur_du_jour(daily, "wind_speed_10m_max", i),
                "source": "open-meteo",
            }
        )
    return resultats


def convertir_openweather(reponse: dict[str, Any]) -> list[dict[str, Any]]:
    """Convertit une réponse One Call 3.0 d'OpenWeather vers le schéma commun.

    One Call fournit daily[].dt (timestamp Unix), temp.max/min, rain (optionnel),
    pop (probabilité 0-1), wind_speed (m/s) et pas d'ensoleillement direct.
    """
    resultats = []
    for jour in reponse.get("daily", []):
        dt = jour.get("dt")
        date_jour = (
            datetime.fromtimestamp(dt, tz=UTC).date().isoformat()
            if dt is not None
            else None
        )
        temp = jour.get("temp", {})
        pluie = jour["rain"] if isinstance(jour.get("rain"), (int, float)) else 0
        pop = jour.get("pop", 0)
        vent_ms = jour.get("wind_speed", 0)
        resultats.append(
            {
                "date": date_jour,
                "temp_max_c": temp.get("max"),
                "temp_min_c": temp.get("min"),
                "pluie_mm": pluie,
                "proba_pluie_pct": round(pop * 100, 1) if pop is not None else None,
                "ensoleillement_h": None,
                "vent_max_kmh": (round(vent_ms * 3.6, 2) if vent_ms is not None else None),
                "source": "openweather",
            }
        )
    return resultats


def meteo_openweather(
    lat: float, lon: float, cle: str, session: requests.Session
) -> list[dict[str, Any]]:
    """Récupère 7 jours de prévisions via OpenWeather One Call 3.0."""
    url = "https://api.openweathermap.org/data/3.0/onecall"
    params: dict[str, str | float] = {
        "lat": lat,
        "lon": lon,
        "exclude": "current,minutely,hourly,alerts",
        "units": "metric",
        "appid": cle,
    }
    reponse = session.get(url, params=params, timeout=30)
    reponse.raise_for_status()
    return convertir_openweather(reponse.json())


def collecter_meteo(
    coordonnees_df: pd.DataFrame,
    lake: DataLake,
    session: requests.Session | None = None,
    pause_s: float = 0.5,
) -> pd.DataFrame:
    """Collecte les prévisions pour chaque ville et les archive.

    OpenWeather est utilisé si une clé est configurée, sinon Open-Meteo.
    Chaque ville est archivée dans son propre fichier (``ville_NN.json``) dès sa réponse, puis
    l'ensemble est consolidé en JSON Lines (une ligne par ville) pour la lecture par Spark. Une
    ville en échec n'interrompt pas les autres ; l'étape signale les villes manquantes par une
    erreur à la fin, et une relance ne redemande que celles-là.

    ``session`` et ``pause_s`` sont injectables : les tests passent un faux client sans pause.
    """
    date_jour = config.date_collecte()
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": config.USER_AGENT})

    cle = config.OPENWEATHER_API_KEY
    chemin_jl = f"raw/meteo/date={date_jour}/previsions.jsonl"

    entrees_brutes = []
    lignes = []
    echecs: dict[str, str] = {}  # ville -> message de l'erreur, pour le bilan final
    for _, row in coordonnees_df.iterrows():
        id_ville = int(row["id_ville"])
        ville = row["ville"]
        lat = row["latitude"]
        lon = row["longitude"]
        # Archive d'UNE ville : écrite dès que la réponse est reçue. Si l'étape s'interrompt
        # (panne réseau, quota), les villes déjà obtenues ne sont pas à redemander.
        chemin_ville = f"raw/meteo/date={date_jour}/ville_{id_ville:02d}.json"

        if lake.existe(chemin_ville):
            entree = json.loads(lake.lire(chemin_ville).decode("utf-8"))
        else:
            # Les coordonnées manquantes viennent du CSV de géocodage : pandas les lit en NaN
            # (nombre flottant), jamais en None ; seul pd.isna reconnaît les deux.
            if pd.isna(lat) or pd.isna(lon):
                # Ville non géocodée : on archive un objet vide et on continue.
                entree = {
                    "id_ville": id_ville,
                    "ville": ville,
                    "previsions": [],
                    "source": "aucune",
                }
            else:
                try:
                    if cle:
                        previsions = meteo_openweather(lat, lon, cle, session)
                        source = "openweather"
                    else:
                        previsions = meteo_open_meteo(lat, lon, session)
                        source = "open-meteo"
                except (requests.RequestException, ValueError) as erreur:
                    # Une ville en échec n'emporte pas les autres : on la note, on n'archive
                    # rien pour elle (une relance la redemandera) et on passe à la suivante.
                    # L'erreur HTTP de requests cite l'URL complète, donc la clé d'API (paramètre
                    # appid) : elle est masquée avant d'aller dans le journal ou dans le message.
                    message = f"{type(erreur).__name__}: {erreur}"
                    if cle:
                        message = message.replace(cle, "***").replace(quote(cle, safe=""), "***")
                    echecs[ville] = message
                    journal.warning("%s : prévisions non obtenues (%s)", ville, echecs[ville])
                    continue
                entree = {
                    "id_ville": id_ville,
                    "ville": ville,
                    "previsions": previsions,
                    "source": source,
                }
            lake.ecrire(chemin_ville, json.dumps(entree, ensure_ascii=False))
            # Pause entre villes pour ne pas surcharger le fournisseur gratuit.
            time.sleep(pause_s)

        entrees_brutes.append(entree)
        for prev in entree["previsions"]:
            lignes.append({"id_ville": id_ville, **prev})

    # Écriture atomique du fichier JSON Lines des villes obtenues (même en cas d'échecs
    # partiels : c'est la trace de ce qui a été collecté).
    contenu_jl = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entrees_brutes)
    lake.ecrire(chemin_jl, contenu_jl)
    if echecs:
        # Le fichier consolidé serait incomplet : on le dit plutôt que de laisser l'ETL classer
        # des destinations sans météo. Relancer l'étape ne redemande que les villes manquantes.
        raise RuntimeError(
            f"prévisions manquantes pour {len(echecs)} ville(s) : {echecs} ; "
            "relancer l'étape reprend uniquement ces villes"
        )
    return pd.DataFrame(lignes, columns=SCHEMA_PREVISION)
