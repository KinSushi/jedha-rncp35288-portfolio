# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Journal des requêtes HTTP d'une collecte : la preuve de ce que chaque service a répondu.

Pourquoi : le rapport de collecte de Booking n'avait pas gardé le statut HTTP des réponses, et
l'on n'a pu que supposer ce que le site avait répondu. Pour les autres sources (page des
destinations, Nominatim, Open-Meteo, Overpass, robots.txt), ce module consigne, pour CHAQUE
requête sortante de la bibliothèque ``requests`` pendant une collecte : l'heure, la méthode,
l'hôte, le chemin, le statut HTTP, la taille de la réponse et la durée, ou le type de l'erreur.
Le journal est archivé dans la zone « raw » du data lake (``raw/journal_http/date=.../``) ; une
collecte relancée le même jour le complète au lieu de l'écraser.

Ce que le journal ne contient JAMAIS : le corps des requêtes et la plupart des paramètres
d'adresse. Seuls des paramètres de la liste blanche ci-dessous (nom de ville, coordonnées...)
sont gardés ; une clé d'API (``appid``) ou tout autre paramètre est écarté. Le robot Scrapy de
Booking tourne dans un autre processus et tient son propre rapport.

CHOIX
-----
- **Retenu : intercepter ``Session.send``**, par où passent toutes les requêtes de la bibliothèque
  (``requests.get`` comme les sessions des collecteurs), puis la restaurer. *Écarté* : ajouter un
  appel de journal dans chaque collecteur, qu'un nouveau collecteur pourrait oublier.
- **Retenu : une liste blanche de paramètres** (``PARAMETRES_CONSERVES``). *Écarté* : une liste
  noire (retirer ``appid``), qui laisserait passer tout secret qu'on n'aurait pas pensé à nommer.
- **Retenu : ne garder du corps d'une requête que sa taille** et, pour Overpass, le centre de
  recherche (``around:8000,lat,lon``) qui dit de quelle ville il s'agit. *Écarté* : consigner le
  corps.
- **Retenu : compléter le journal du jour** au lieu de l'écraser. *Écarté* : l'écraser, qui ferait
  disparaître la trace des premiers essais d'une collecte reprise après une panne.

OÙ
---
- **Place** : enveloppe des étapes de collecte du pipeline (kayak/pipeline.py:127), active
  seulement si l'une d'elles est demandée.
- **Appelants** : ``kayak/pipeline.py``. Le notebook relit le fichier produit (section 2).
- **Dépendances** : ``requests``, ``kayak.config`` (date de collecte),
  ``kayak.stockage.data_lake``.

COMMENT
-------
- ``journaliser_requetes`` remplace ``Session.send`` par une version qui note, pour chaque envoi,
  une entrée construite par ``_entree`` (heure UTC, méthode, hôte, chemin, paramètres filtrés,
  octets envoyés, durée) puis le statut, les octets reçus et la date serveur, ou le type de
  l'erreur qu'elle relance ; à la sortie du bloc, même sur exception, elle restaure ``send`` et
  écrit le journal si au moins une requête a eu lieu.
- **Preuves** : chaque requête est consignée avec son statut (tests/test_journal.py:57) ; ``send``
  est restauré (tests/test_journal.py:111) ; le journal d'un essai précédent est complété
  (tests/test_journal.py:101).
- **Refus** (tests inverses) : aucun secret ni corps dans le journal (tests/test_journal.py:73) ;
  les paramètres hors liste blanche sont écartés et la requête reste intacte
  (tests/test_journal_inverses.py:127) ; une erreur réseau est consignée puis relancée
  (tests/test_journal.py:91).

POURQUOI
--------
- La raison d'être est un constat du projet : le rapport de Booking du 10/10/2026 n'avait pas le
  statut HTTP (voir la section 3 du notebook, qui le vérifie sur le rapport archivé).
- L'hôte est consigné SANS les identifiants que l'URL pourrait porter, et la taille d'un corps
  texte en octets : deux défauts relevés par les tests de la mise en conformité, corrigés et
  couverts par tests/test_corrections_conformite.py:48 et tests/test_corrections_conformite.py:57.
- La liste blanche applique le principe « on garde ce qu'on a nommé, jamais ce qu'on a oublié
  d'exclure » ; test de référence : tests/test_journal_inverses.py:146.
- La politique d'usage de Nominatim (https://operations.osmfoundation.org/policies/nominatim/,
  lecture non consignée, à revérifier) demande un User-Agent identifiable et au plus une requête par
  seconde : le journal permet de vérifier le rythme réel de la collecte (durée et horodatage de
  chaque appel).

JOURNALISATION
--------------
- Ce module EST le journal : il n'utilise pas ``logging``. Il écrit dans le lake un fichier JSON
  Lines, une ligne par requête, avec les champs ``horodatage_utc``, ``methode``, ``hote``,
  ``chemin``, ``parametres``, ``octets_envoyes``, ``duree_s``, ``statut_http``, ``octets_recus``,
  ``date_serveur`` et ``erreur``.
- Aucun niveau de gravité : une erreur réseau est une entrée à ``statut_http`` nul dont le champ
  ``erreur`` porte le nom du type de l'exception, qui est ensuite relancée.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit

import requests

from kayak import config
from kayak.stockage.data_lake import DataLake

# Paramètres d'adresse sans danger, utiles pour savoir de quelle ville il s'agit. Tout le
# reste (clé d'API, jeton...) est écarté : on garde une liste blanche, jamais une liste noire.
PARAMETRES_CONSERVES = {
    "q",
    "format",
    "limit",
    "countrycodes",
    "latitude",
    "longitude",
    "lat",
    "lon",
    "timezone",
    "forecast_days",
}


def _entree(requete: requests.PreparedRequest, debut: float) -> dict[str, Any]:
    """Début d'une entrée de journal, avec les champs communs à tous les cas."""
    morceaux = urlsplit(requete.url or "")
    parametres = [(c, v) for c, v in parse_qsl(morceaux.query) if c in PARAMETRES_CONSERVES]
    corps = requete.body
    taille_corps = 0
    # Une requête Overpass part en POST : le corps contient la requête, dont on ne garde que le
    # centre de recherche (« around:8000,lat,lon »), pour savoir de quelle ville il s'agit.
    if isinstance(corps, bytes | str) and corps:
        octets_corps = corps if isinstance(corps, bytes) else corps.encode("utf-8")
        taille_corps = len(octets_corps)  # en octets, comme l'en-tête Content-Length
        texte = unquote_plus(octets_corps.decode("utf-8", "replace"))
        autour = re.search(r"around:\d+,(-?[\d.]+),(-?[\d.]+)", texte)
        if autour:
            parametres.append(("autour", f"{autour.group(1)},{autour.group(2)}"))
    return {
        "horodatage_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "methode": requete.method,
        # Sans les identifiants que l'URL pourrait porter (« utilisateur:secret@hôte »).
        "hote": morceaux.netloc.rpartition("@")[2],
        "chemin": morceaux.path,
        "parametres": urlencode(parametres),
        "octets_envoyes": taille_corps,  # la taille seulement, jamais le corps
        "duree_s": round(time.perf_counter() - debut, 3),
    }


@contextlib.contextmanager
def journaliser_requetes(lake: DataLake) -> Iterator[list[dict[str, Any]]]:
    """Consigne les requêtes ``requests`` émises dans le bloc, puis archive le journal.

    On remplace temporairement ``Session.send``, par où passent toutes les requêtes de la
    bibliothèque (``requests.get`` comme les sessions des collecteurs), puis on la restaure.
    Le journal est écrit dans le lake à la sortie du bloc, même si une étape a échoué : c'est
    justement alors qu'on en a besoin. Rien n'est écrit si aucune requête n'a été émise.
    """
    entrees: list[dict[str, Any]] = []
    envoyer = requests.sessions.Session.send

    def envoyer_journalise(
        self: requests.Session, requete: requests.PreparedRequest, **options: Any
    ) -> requests.Response:
        debut = time.perf_counter()
        try:
            reponse = envoyer(self, requete, **options)
        except Exception as erreur:
            entrees.append(
                {**_entree(requete, debut), "statut_http": None, "erreur": type(erreur).__name__}
            )
            raise
        entrees.append(
            {
                **_entree(requete, debut),
                "statut_http": reponse.status_code,
                "octets_recus": len(reponse.content),
                "date_serveur": reponse.headers.get("Date"),
                "erreur": None,
            }
        )
        return reponse

    requests.sessions.Session.send = envoyer_journalise  # type: ignore[method-assign,assignment]
    try:
        yield entrees
    finally:
        requests.sessions.Session.send = envoyer  # type: ignore[method-assign]
        if entrees:
            chemin = f"raw/journal_http/date={config.date_collecte()}/journal_http.jsonl"
            # Une collecte relancée le même jour (reprise après une panne) COMPLÈTE le journal :
            # écraser les lignes précédentes ferait disparaître la trace des premiers essais.
            avant = lake.lire(chemin).decode("utf-8") if lake.existe(chemin) else ""
            lake.ecrire(
                chemin,
                avant + "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entrees),
            )
