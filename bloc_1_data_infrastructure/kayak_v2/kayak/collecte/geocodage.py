# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Géocodage des destinations avec l'API Nominatim (OpenStreetMap).

Pourquoi Nominatim : l'énoncé le recommande ; c'est un service ouvert, sans inscription. Sa
politique d'usage impose au plus une requête par seconde et un User-Agent qui identifie
l'application : ce module respecte les deux. La réponse BRUTE de l'API est archivée dans la zone
« raw » du data lake (traçabilité : on peut toujours revenir à ce que l'API a renvoyé) et
réutilisée si elle existe déjà pour la même date (aucun appel inutile lors d'une relance).

CHOIX
-----
- **Retenu : Nominatim**, recommandé par l'énoncé, ouvert et sans inscription. Une requête par
  ville, ``limit=1`` et ``countrycodes=fr`` : le premier résultat français suffit au centre d'une
  destination. *Écarté* : un géocodeur commercial, qui demanderait une clé.
- **Retenu : archiver la réponse brute de chaque ville et la réutiliser** si elle existe pour la
  même date. *Écarté* : réinterroger à chaque relance, qui violerait l'esprit de la politique
  d'usage (au plus une requête par seconde) et rendrait les coordonnées instables d'une exécution
  à l'autre.
- **Retenu : une pause de 1,1 s après chaque appel réseau** (et aucune pour une archive
  réutilisée). *Écarté* : aucune pause, qui dépasserait la limite d'une requête par seconde.
- **Retenu : écrire un CSV de travail** (``coordonnees.csv``, fins de ligne LF) à côté des
  réponses brutes : c'est l'entrée de l'ETL.

OÙ
---
- **Place** : deuxième étape de la collecte (kayak/pipeline.py:146), après les destinations, avant
  la météo et les hôtels.
- **Appelants** : ``kayak/pipeline.py``. **Dépendances** : ``requests``, pandas, ``kayak.config``
  (User-Agent, date), ``kayak.stockage.data_lake``.

COMMENT
-------
- ``interroger`` envoie la requête et rend le JSON brut ; ``interpreter`` extrait latitude,
  longitude et nom complet du premier résultat (trois ``None`` si la réponse est vide) ;
  ``geocoder`` enchaîne les deux ; ``collecter_coordonnees`` parcourt les destinations, archive
  ``ville_NN.json`` et écrit le CSV.
- **Preuves** : les paramètres et le JSON brut (tests/test_geocodage.py:238) ; la conversion
  (tests/test_geocodage.py:154) ; le CSV (tests/test_geocodage.py:536) ; le User-Agent part
  sous son nom écrit « User-Agent » (tests/test_geocodage.py:522).
- **Refus** (tests inverses) : une réponse sans latitude est refusée (tests/test_geocodage.py:187)
  ; un statut HTTP d'erreur est propagé (tests/test_geocodage.py:262) ; une archive réutilisée ne
  déclenche ni appel ni pause (tests/test_geocodage.py:313) ; une archive mal formée est refusée
  (tests/test_geocodage.py:394).

POURQUOI
--------
- Une requête par seconde au plus et un User-Agent qui identifie l'application : politique d'usage
  de Nominatim, https://operations.osmfoundation.org/policies/nominatim/ (lecture non
  consignée, à revérifier ; elle fixe un maximum absolu d'une requête par seconde et exige une
  identification de l'application).
- La pause de 1,1 s (et non 1,0 s) laisse une marge au délai de l'appel ; constante dans le code :
  kayak/collecte/geocodage.py:124. Source du choix de la marge : inconnue.
- Les coordonnées ne sont pas parfaitement stables d'une collecte à l'autre (deux villes ont
  changé de centre entre le 10/10 et le 11/10) : constat du notebook, section 6.

JOURNALISATION
--------------
- Aucun message ``logging``. La trace est l'ARCHIVE de chaque réponse brute dans le lake et la
  requête HTTP est consignée par ``kayak/collecte/journal.py`` (hôte, paramètres de la liste
  blanche, statut).
- Les erreurs (statut HTTP, JSON illisible, réseau) sont des exceptions propagées à l'appelant.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pandas as pd
import requests

from kayak import config
from kayak.stockage.data_lake import DataLake

URL_NOMINATIM = "https://nominatim.openstreetmap.org/search"


def interroger(ville: str, session: requests.Session) -> list[dict[str, Any]]:
    """Interroge Nominatim pour « <ville>, France » ; renvoie la réponse JSON brute."""
    parametres: dict[str, str | int] = {
        "q": f"{ville}, France",
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "fr",
    }
    reponse = session.get(URL_NOMINATIM, params=parametres, timeout=30)
    reponse.raise_for_status()
    resultats: list[dict[str, Any]] = reponse.json()
    return resultats


def interpreter(reponse: list[dict[str, Any]]) -> dict[str, Any]:
    """Extrait latitude, longitude et nom complet du premier résultat (ou des None)."""
    if not reponse:
        return {"latitude": None, "longitude": None, "nom_complet": None}
    premier = reponse[0]
    return {
        "latitude": float(premier["lat"]),
        "longitude": float(premier["lon"]),
        "nom_complet": premier.get("display_name"),
    }


def geocoder(ville: str, session: requests.Session) -> dict[str, Any]:
    """Coordonnées d'une ville française (latitude, longitude, nom complet)."""
    return interpreter(interroger(ville, session))


def collecter_coordonnees(destinations_df: pd.DataFrame, lake: DataLake) -> pd.DataFrame:
    """Géocode chaque destination ; archive une réponse brute par ville dans le data lake."""
    date_jour = config.date_collecte()
    session = requests.Session()
    session.headers.update({"User-Agent": config.USER_AGENT})
    lignes = []
    for _, ligne in destinations_df.iterrows():
        id_ville, ville = int(ligne["id_ville"]), ligne["ville"]
        chemin = f"raw/geocodage/date={date_jour}/ville_{id_ville:02d}.json"
        if lake.existe(chemin):
            reponse = json.loads(lake.lire(chemin).decode("utf-8"))
        else:
            reponse = interroger(ville, session)
            lake.ecrire(chemin, json.dumps(reponse, ensure_ascii=False, indent=2))
            time.sleep(1.1)  # politique de Nominatim : une requête par seconde au plus
        lignes.append({"id_ville": id_ville, "ville": ville, **interpreter(reponse)})
    coordonnees = pd.DataFrame(lignes)
    # Table de travail (CSV) à côté des réponses brutes : c'est l'entrée de l'ETL.
    # Fins de ligne « \n » partout (pandas écrit « \r\n » sous Windows) : le même fichier
    # sur tous les systèmes.
    lake.ecrire(
        f"raw/geocodage/date={date_jour}/coordonnees.csv",
        coordonnees.to_csv(index=False, lineterminator="\n"),
    )
    return coordonnees
