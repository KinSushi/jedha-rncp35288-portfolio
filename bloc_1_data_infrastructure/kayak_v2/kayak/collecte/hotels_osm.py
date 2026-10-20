# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Source de repli pour les hôtels : OpenStreetMap, via l'API Overpass.

Pourquoi ce module existe : Booking.com protège ses pages de résultats par un contrôle
anti-robot (AWS WAF, défi JavaScript constaté le 10/10/2026). Le robot Scrapy du projet le
détecte et s'arrête : contourner ce contrôle serait contraire aux conditions d'utilisation du
site et au cadre légal de la collecte (compétence C1.3 : collecter « dans le respect des
normes »). Pour que le pipeline livre tout de même des hôtels réels, cette étape interroge
OpenStreetMap :
- données ouvertes sous licence ODbL (réutilisation autorisée, attribution « © les
  contributeurs d'OpenStreetMap ») ;
- API Overpass prévue pour l'accès automatisé (une requête par ville, une seconde d'écart) ;
- des établissements, pas des personnes ; et leurs moyens de contact (courriel, téléphone,
  étiquettes ``contact:*``) sont retirés de chaque réponse dès sa réception, avant l'archivage
  (``kayak/collecte/minimisation.py``).
Limite assumée : OpenStreetMap ne contient ni note des clients ni nombre d'avis ; le classement
des hôtels repose alors sur le nombre d'étoiles quand il est renseigné.

Historique de la requête (important pour lire la base livrée) : la première version demandait
``out center tags 40``, soit les 40 premiers éléments seulement. Overpass renvoie les points
(node) avant les contours (way) puis coupe : pour 23 villes sur 35 la réponse atteignait
exactement 40 éléments, et un « top 20 » calculé dessus portait sur un échantillon tronqué, pas
sur tous les hôtels de la zone. La requête actuelle ne coupe plus rien (``out center tags;``) et
la réponse est contrôlée : une réponse partielle signalée par le serveur est refusée et
redemandée. Les réponses déjà archivées par l'ancienne requête (collecte du 10/10/2026) restent
lisibles (``--reconvertir``), mais elles sont signalées comme potentiellement tronquées ; les
résultats du projet reposent sur la collecte complète suivante (voir le README).

Second défaut corrigé : le rayon de 8 km autour de Strasbourg traversait le Rhin et ramenait des
hôtels de Kehl (Allemagne), que le simple rectangle « France » de l'ETL laissait passer. La
requête restreint maintenant la recherche à la zone administrative de la France
(``area["ISO3166-1"="FR"]``) ; l'étiquette ``addr:country`` est en outre conservée (champ
``pays``) pour que l'ETL écarte tout hôtel déclaré hors de France.

Usage hors ligne (aucun appel réseau) : reconstruire ``hotels.jsonl`` depuis les réponses brutes
déjà archivées, par exemple après l'ajout d'un champ à la conversion :
    python -m kayak.collecte.hotels_osm --reconvertir --date 2026-10-10

CHOIX
-----
- **Retenu : OpenStreetMap par Overpass**, une requête par ville, après le refus de Booking.com
  (contrôle anti-robot respecté). *Écarté* : contourner le contrôle anti-robot.
- **Retenu : aucune limite de nombre dans la requête** (``out center tags;``) et refus d'une
  réponse partielle annoncée par le serveur. *Écarté* : ``out center tags 40``, qui tronquait 23
  villes sur 35.
- **Retenu : restreindre la recherche à la zone administrative de la France** et garder
  ``addr:country``. *Écarté* : le rectangle seul, qui laissait passer les hôtels de Kehl.
- **Retenu : réessayer avec une attente croissante puis basculer sur un serveur miroir.** *Écarté*
  : abandonner au premier 504, alors que le serveur principal a été « occupé » une fois sur deux
  le 11/10/2026.
- **Retenu : archiver la réponse brute de chaque ville et pouvoir la rejouer hors ligne**
  (``reconvertir_depuis_archive``). *Écarté* : reconvertir en interrogeant de nouveau le service.
- **Retenu : minimiser la réponse dans ``interroger``**, au plus près de sa réception : rien de ce
  qui suit (archive, conversion) ne voit un courriel ou un téléphone. *Écarté* : filtrer
  seulement à la conversion, qui laissait les contacts dans l'archive brute.

OÙ
---
- **Place** : étape ``osm`` de la collecte, repli pour les villes que Booking n'a pas servies
  (kayak/pipeline.py:171) ; son ``hotels.jsonl`` est lu par l'ETL.
- **Appelants** : ``kayak/pipeline.py``, la ligne de commande ``--reconvertir`` et le notebook
  (``diagnostic_troncature``, lecture seule). **Dépendances** : ``requests``, pandas,
  ``kayak.config``, ``kayak.stockage.data_lake``, ``kayak.collecte.minimisation``.

COMMENT
-------
- ``requete_overpass`` écrit la requête ; ``interroger`` l'envoie en gérant attentes et miroir,
  puis rend la réponse minimisée ; ``verifier_reponse`` refuse une réponse partielle ;
  ``convertir`` met un élément au schéma commun des hôtels ; ``construire_hotels`` parcourt les
  villes (réseau si une session est donnée, archives seules sinon) ; ``diagnostic_troncature``
  compte les archives de l'ancienne requête.
- **Preuves directes** : la requête sans plafond (tests/test_osm.py:121), limitée à la France
  (tests/test_osm.py:108) ; la conversion d'un point et d'un contour (tests/test_osm.py:38) ; la
  réponse rendue par ``interroger`` est déjà minimisée (tests/test_minimisation.py:207).
- **Refus** (tests inverses) : une réponse partielle est refusée (tests/test_osm.py:137) ; la
  reconversion n'ouvre jamais de session (tests/test_osm.py:245) ; le basculement sur le miroir
  suit les attentes prévues (tests/test_osm_inverses.py:472) ; une archive illisible lève une
  erreur (tests/test_osm_inverses.py:705).

POURQUOI
--------
- API Overpass et accès automatisé : https://wiki.openstreetmap.org/wiki/Overpass_API
  (lecture non consignée, à revérifier). Licence ODbL des données : texte de
  la licence, https://opendatacommons.org/licenses/odbl/ (non relu dans ce travail).
- La troncature à 40 éléments (23 villes sur 35) est mesurée par le notebook sur les archives du
  10/10/2026 (section 3) et par tests/test_osm.py:261.
- Les attentes (0, 5, 20 et 45 s) et le miroir sont des choix de politesse du projet, constantes
  kayak/collecte/hotels_osm.py:122 et kayak/collecte/hotels_osm.py:126 ; source externe : inconnue.

JOURNALISATION
--------------
- Logger ``kayak.collecte.hotels_osm``. Au niveau WARNING : une archive issue de l'ancienne
  requête plafonnée (kayak/collecte/hotels_osm.py:276) et une ville sans archive en mode hors
  ligne (« aucune archive, ville ignorée »). En ligne de commande : INFO via
  ``logging.basicConfig``, et un message imprimé « N hôtels reconstruits depuis les réponses
  archivées (aucun appel réseau) ».
- Les requêtes HTTP sont consignées par ``kayak/collecte/journal.py``.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import time
from typing import Any

import pandas as pd
import requests

from kayak import config
from kayak.collecte.minimisation import minimiser_reponse
from kayak.stockage.data_lake import DataLake

journal = logging.getLogger(__name__)

# Serveur principal et miroir public : le serveur principal est souvent saturé (erreurs 429,
# 504) ; on réessaie avec une attente croissante, puis on passe au miroir.
SERVEURS_OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
ATTENTES_S = [5, 20, 45]  # attentes entre deux essais sur le même serveur
RAYON_M = config.RAYON_AIRE_M  # hôtels situés à moins de 8 km du centre de la destination
# Plafond de l'ANCIENNE requête (« out center tags 40 ») : une archive sans marque de requête
# dont la réponse compte exactement ce nombre d'éléments a probablement été tronquée.
PLAFOND_ANCIENNE_REQUETE = 40
DECALAGE_WAY = 10**12  # sépare les identifiants des points (node) et des contours (way)
MARQUE_REQUETE = "kayak_requete"  # clé ajoutée aux archives : la requête qui les a produites


class ReponseIncomplete(RuntimeError):
    """Overpass a répondu HTTP 200 mais signale une exécution interrompue (délai, mémoire)."""


def requete_overpass(latitude: float, longitude: float) -> str:
    """Requête Overpass QL : hôtels nommés (points et bâtiments) autour d'un point.

    Aucune limite de nombre : la requête rend tous les hôtels de l'aire, points ET contours
    (``out center`` donne un point central aux contours). Le filtre ``["name"]`` écarte à la
    source les établissements sans nom, que la conversion ignorerait de toute façon. Le filtre
    ``(area.pays)`` limite la recherche à la zone administrative de la France : sans lui, le
    rayon de 8 km déborde des frontières (Kehl pour Strasbourg). Le délai de 90 s laisse au
    serveur le temps de répondre pour les grandes villes.
    """
    autour = f"(around:{RAYON_M},{latitude},{longitude})"
    return (
        "[out:json][timeout:90];"
        'area["ISO3166-1"="FR"][admin_level=2]->.pays;'
        f'(node["tourism"="hotel"]["name"](area.pays){autour};'
        f'way["tourism"="hotel"]["name"](area.pays){autour};);'
        "out center tags;"
    )


def verifier_reponse(reponse: dict[str, Any]) -> dict[str, Any]:
    """Refuse une réponse Overpass partielle.

    Quand une requête dépasse son délai ou sa mémoire, Overpass répond quand même HTTP 200 avec
    les éléments déjà trouvés et un champ ``remark`` qui commence par « runtime error ». Sans
    ce contrôle, une troncature silencieuse recommencerait, cette fois côté serveur.
    """
    remarque = str(reponse.get("remark", ""))
    if "runtime error" in remarque.lower():
        raise ReponseIncomplete(remarque)
    return reponse


def archive_reutilisable(reponse: dict[str, Any]) -> bool:
    """Une archive déjà présente peut-elle être relue telle quelle ?

    Oui si elle porte la marque de la nouvelle requête (non tronquée), ou si l'ancienne requête
    n'a pas atteint son plafond (moins de 40 éléments : la réponse était complète).
    """
    if MARQUE_REQUETE in reponse:
        return True
    return len(reponse.get("elements", [])) < PLAFOND_ANCIENNE_REQUETE


def convertir(
    element: dict[str, Any], id_ville: int, ville: str, date_collecte: str
) -> dict[str, Any] | None:
    """Élément OpenStreetMap → schéma commun des hôtels (None si l'hôtel n'a pas de nom)."""
    etiquettes = element.get("tags", {})
    if not etiquettes.get("name"):
        return None
    centre = element.get("center", element)  # un contour (way) a un « center »
    nature = element.get("type", "node")
    identifiant = element["id"] + (DECALAGE_WAY if nature == "way" else 0)
    etoiles = etiquettes.get("stars", "")
    adresse = " ".join(
        etiquettes[cle]
        for cle in ("addr:housenumber", "addr:street", "addr:postcode", "addr:city")
        if etiquettes.get(cle)
    )
    return {
        "hotel_id": identifiant,
        "nom": etiquettes["name"],
        "url": etiquettes.get("website")
        or etiquettes.get("contact:website")
        or f"https://www.openstreetmap.org/{nature}/{element['id']}",
        "latitude": centre.get("lat"),
        "longitude": centre.get("lon"),
        "adresse": adresse or None,
        # Commune déclarée dans OpenStreetMap (addr:city), gardée à part de l'adresse : elle
        # permet à l'ETL de repérer les hôtels d'une autre commune que la destination. Elle
        # n'est renseignée que pour une partie des établissements.
        "commune": etiquettes.get("addr:city"),
        # Pays déclaré (addr:country, « DE » pour Kehl) : renseigné pour peu d'hôtels, mais
        # quand il l'est et qu'il n'est pas « FR », l'ETL écarte l'hôtel.
        "pays": etiquettes.get("addr:country"),
        "note": None,  # absente d'OpenStreetMap
        "nb_avis": None,
        "etoiles": int(etoiles[0]) if etoiles[:1].isdigit() else None,
        "description": etiquettes.get("description"),
        "id_ville": id_ville,
        "ville": ville,
        "date_collecte": date_collecte,
        "source": "openstreetmap",
    }


def interroger(session: requests.Session, requete: str) -> dict[str, Any]:
    """Envoie la requête Overpass ; réessaie en cas de saturation, puis bascule sur le miroir.

    Sont réessayées : les erreurs de saturation (429, 5xx), les coupures réseau, les corps de
    réponse qui ne sont pas du JSON et les réponses partielles signalées par le serveur.
    """
    derniere_erreur: Exception | None = None
    for serveur in SERVEURS_OVERPASS:
        for attente in [0, *ATTENTES_S]:
            time.sleep(attente)
            try:
                resultat = session.post(serveur, data={"data": requete}, timeout=120)
                if resultat.status_code in (
                    429,
                    500,
                    502,
                    503,
                    504,
                ):  # serveur saturé : on réessaie
                    derniere_erreur = requests.HTTPError(f"{resultat.status_code} {serveur}")
                    continue
                resultat.raise_for_status()
                # Minimisation dès la réception : ni courriel ni téléphone n'est archivé.
                return minimiser_reponse(verifier_reponse(resultat.json()))
            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.JSONDecodeError,
                ReponseIncomplete,
            ) as erreur:
                derniere_erreur = erreur
    raise RuntimeError(f"Overpass indisponible après plusieurs essais : {derniere_erreur}")


def _reponse_de_ville(
    lake: DataLake,
    chemin_brut: str,
    ligne: pd.Series,
    session: requests.Session | None,
) -> dict[str, Any] | None:
    """Réponse Overpass d'une ville : archive réutilisable, sinon requête réseau.

    Avec ``session=None`` (mode hors ligne), aucune requête n'est faite : on relit l'archive
    telle quelle, en signalant celle qui vient de l'ancienne requête plafonnée, et on renvoie
    None si la ville n'a pas d'archive.
    """
    reponse = None
    if lake.existe(chemin_brut):
        reponse = json.loads(lake.lire(chemin_brut).decode("utf-8"))
        if not archive_reutilisable(reponse):
            journal.warning(
                "%s : archive issue de l'ancienne requête plafonnée à %d éléments, "
                "possiblement tronquée",
                chemin_brut,
                PLAFOND_ANCIENNE_REQUETE,
            )
            if session is not None:
                reponse = None  # en collecte, on la remplace par une réponse complète
    if reponse is None and session is not None:
        requete = requete_overpass(ligne["latitude"], ligne["longitude"])
        reponse = interroger(session, requete)
        reponse[MARQUE_REQUETE] = requete  # trace de la requête qui a produit cette archive
        lake.ecrire(chemin_brut, json.dumps(reponse, ensure_ascii=False))
        time.sleep(1.0)  # usage raisonnable de l'API publique
    return reponse


def construire_hotels(
    coordonnees_df: pd.DataFrame,
    lake: DataLake,
    session: requests.Session | None,
) -> list[dict[str, Any]]:
    """Convertit les réponses Overpass de chaque ville au schéma commun (réseau si session)."""
    date_jour = config.date_collecte()
    hotels = []
    for _, ligne in coordonnees_df.dropna(subset=["latitude", "longitude"]).iterrows():
        id_ville, ville = int(ligne["id_ville"]), ligne["ville"]
        chemin_brut = f"raw/osm/date={date_jour}/ville_{id_ville:02d}.json"
        reponse = _reponse_de_ville(lake, chemin_brut, ligne, session)
        if reponse is None:
            journal.warning("%s : aucune archive, ville ignorée (mode hors ligne)", ville)
            continue
        for element in reponse.get("elements", []):
            hotel = convertir(element, id_ville, ville, date_jour)
            if hotel is not None:
                hotels.append(hotel)
    return hotels


def ecrire_hotels(lake: DataLake, hotels: list[dict[str, Any]]) -> None:
    """Écrit ``hotels.jsonl`` (une ligne par hôtel) dans la zone brute de la date courante."""
    lake.ecrire(
        f"raw/osm/date={config.date_collecte()}/hotels.jsonl",
        "".join(json.dumps(h, ensure_ascii=False) + "\n" for h in hotels),
    )


def collecter_hotels_osm(coordonnees_df: pd.DataFrame, lake: DataLake) -> pd.DataFrame:
    """Interroge Overpass pour chaque ville ; archive les réponses brutes et le résultat."""
    session = requests.Session()
    session.headers.update({"User-Agent": config.USER_AGENT})
    hotels = construire_hotels(coordonnees_df, lake, session)
    ecrire_hotels(lake, hotels)
    return pd.DataFrame(hotels)


def diagnostic_troncature(lake: DataLake) -> dict[str, Any]:
    """Combien de villes ont une archive probablement tronquée par l'ancienne requête ?

    Une archive est « de l'ancienne requête » quand elle ne porte pas la marque de la nouvelle ;
    elle est « plafonnée » quand, en plus, sa réponse compte au moins 40 éléments (le plafond de
    l'ancienne requête). Renvoie les comptes, pour que les cartes et le notebook puissent dire
    honnêtement sur quel échantillon porte le top 20.
    """
    archives = [
        chemin
        for chemin in lake.lister(f"raw/osm/date={config.date_collecte()}/")
        if re.search(r"ville_\d+\.json$", chemin)
    ]
    anciennes = plafonnees = 0
    for chemin in sorted(archives):
        reponse = json.loads(lake.lire(chemin).decode("utf-8"))
        if MARQUE_REQUETE not in reponse:
            anciennes += 1
            if len(reponse.get("elements", [])) >= PLAFOND_ANCIENNE_REQUETE:
                plafonnees += 1
    return {
        "villes": len(archives),
        "archives_ancienne_requete": anciennes,
        "villes_plafonnees": plafonnees,
        "plafond": PLAFOND_ANCIENNE_REQUETE,
    }


def reconvertir_depuis_archive(lake: DataLake) -> pd.DataFrame:
    """Reconstruit ``hotels.jsonl`` depuis les réponses brutes archivées, SANS aucun réseau.

    Utile quand la conversion évolue (ex. ajout de la commune) : la zone brute est la source de
    vérité, on la rejoue sans interroger de nouveau le service.
    """
    chemin = f"raw/geocodage/date={config.date_collecte()}/coordonnees.csv"
    coordonnees = pd.read_csv(io.BytesIO(lake.lire(chemin)))
    hotels = construire_hotels(coordonnees, lake, session=None)
    ecrire_hotels(lake, hotels)
    return pd.DataFrame(hotels)


def main() -> None:
    """Point d'entrée en ligne de commande : reconversion hors ligne d'une collecte archivée."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    analyseur = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    analyseur.add_argument("--reconvertir", action="store_true", required=True)
    analyseur.add_argument("--date", default=None, help="date de collecte AAAA-MM-JJ")
    arguments = analyseur.parse_args()
    if arguments.date:
        os.environ["KAYAK_DATE_COLLECTE"] = arguments.date
    hotels = reconvertir_depuis_archive(DataLake())
    print(f"{len(hotels)} hôtels reconstruits depuis les réponses archivées (aucun appel réseau)")


if __name__ == "__main__":
    main()
