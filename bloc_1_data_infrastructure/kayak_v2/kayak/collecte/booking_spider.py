# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Collecte des hôtels sur Booking.com via Scrapy.

Le spider parcourt une seule page de résultats par ville (25 hôtels max)
avec le filtre 'hôtels' activé. L'extraction privilégie le JSON embarqué
dans la page ; en l'absence de ce JSON, un repli BeautifulSoup est utilisé.
Conformément au RGPD, les annonces d'hôtes particuliers (hostTraderLabel
PRIVATE) sont exclues, ainsi que les propriétés de test.

Ce qui est vérifié et ce qui ne l'est pas :
- robots.txt est archivé daté dans le data lake avant toute requête, et le verdict pour l'URL
  demandée est calculé règle par règle (module ``robots``) ;
- le statut HTTP de CHAQUE réponse est consigné dans le rapport de collecte (y compris les
  refus 403/429/5xx et les erreurs réseau), pour qu'on puisse prouver ce que le site a répondu ;
- l'extraction des hôtels (``extraire_hotels``) n'a JAMAIS été validée sur une page réelle : le
  10/10/2026, Booking.com a opposé son contrôle anti-robot à toutes les requêtes. Sa structure
  JSON est une hypothèse, testée seulement sur du HTML fabriqué (voir tests/test_booking.py).

CHOIX
-----
- **Retenu : Scrapy**, avec un robot poli (``ROBOTSTXT_OBEY``, une requête à la fois, délai
  nominal de 3 s avec aléa de ±50 %, AutoThrottle, User-Agent qui identifie le projet) et UNE page
  de résultats par ville. *Écarté* : requêter en rafale ou avec un User-Agent anonyme.
- **Retenu : s'arrêter net devant un contrôle anti-robot** (statut 202 ou marqueur
  ``AwsWafIntegration``) et le consigner. *Écarté* : tenter de résoudre ou de contourner le défi,
  contraire aux conditions d'utilisation du site et à la collecte loyale (compétence C1.3).
- **Retenu : Scrapy dans un sous-processus** (``lancer_collecte_booking``). *Écarté* : le lancer
  dans le processus courant, car le moteur réseau Twisted ne démarre qu'une fois par processus et
  le robot échouerait à la seconde exécution d'une cellule de notebook.
- **Retenu : laisser passer tous les statuts HTTP jusqu'à ``parse``** pour les consigner. *Écarté*
  : le comportement par défaut de Scrapy, qui écarte les 4xx et 5xx avant ``parse`` et ferait
  disparaître le refus du rapport.
- **Retenu : exclure les hôtes particuliers et les propriétés de test** (minimisation RGPD).

OÙ
---
- **Place** : étape ``booking`` de la collecte (kayak/pipeline.py:156). Dans ce projet, elle n'a
  pas été relancée le 11/10/2026 : les hôtels viennent d'OpenStreetMap
  (``kayak/collecte/hotels_osm.py``).
- **Appelants** : ``kayak/pipeline.py`` et, en sous-processus, ``python -m
  kayak.collecte.booking_spider``.
- **Dépendances** : Scrapy, BeautifulSoup, pandas, ``kayak.collecte.robots``, ``kayak.config``,
  ``kayak.stockage.data_lake``.

COMMENT
-------
- ``extraire_hotels`` lit le JSON embarqué puis, à défaut, le HTML ; ``BookingHotelsSpider`` émet
  une requête par ville, consigne dans ``rapport`` le statut, l'URL, la taille et l'issue de
  chaque réponse, et écrit le rapport à la fermeture ; ``lancer_collecte_booking`` vérifie
  robots.txt, lance le sous-processus et archive hôtels et rapport.
- **Preuves** : champs extraits et filtres (tests/test_booking.py:36) ; rapport d'un défi
  anti-robot (tests/test_booking.py:161) ; réglages Scrapy existants (tests/test_booking.py:227).
- **Refus** (tests inverses) : un robots.txt qui interdit arrête la collecte avant tout
  sous-processus (tests/test_booking_inverses.py:802) ; le défi anti-robot n'est jamais extrait ni
  contourné (tests/test_booking_inverses.py:477) ; un sous-processus en échec n'écrit rien dans le
  lake (tests/test_booking_inverses.py:841).

POURQUOI
--------
- Le 10/10/2026, Booking.com a répondu par un défi AWS WAF à chaque requête (statut 202) : preuve
  dans le rapport d'archive de la première collecte, que le notebook relit (section 3).
- Limite : l'extraction n'est pas validée sur une page réelle ; les tests de ``extraire_hotels``
  sont circulaires (voir l'avertissement en tête de tests/test_booking.py). Source de la structure
  JSON visée : inconnue, c'est une hypothèse.
- ``DOWNLOAD_DELAY_JITTER`` existe depuis Scrapy 2.19 et remplace ``RANDOMIZE_DOWNLOAD_DELAY`` ;
  le test tests/test_booking.py:227 vérifie que chaque réglage existe dans la version installée.

JOURNALISATION
--------------
- Le spider écrit par le logger de Scrapy (``self.logger``), au niveau WARNING pour un contrôle
  anti-robot (kayak/collecte/booking_spider.py:313), une réponse HTTP différente de 200 et une
  erreur avant réponse ; le niveau de Scrapy est INFO.
- La preuve durable est le ``rapport_collecte.json`` archivé dans le lake : pour chaque ville, le
  statut HTTP, l'URL, la taille de la réponse, l'issue et, le cas échéant, le type d'erreur.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import quote

import pandas as pd
import scrapy
from bs4 import BeautifulSoup
from scrapy.crawler import CrawlerProcess
from scrapy.http import Response
from twisted.python.failure import Failure

from kayak import config
from kayak.collecte.robots import archiver_robots, verdict
from kayak.stockage.data_lake import DataLake

URL_RECHERCHE = "https://www.booking.com/searchresults.fr.html"


def extraire_hotels(
    html: str,
    id_ville: int,
    ville: str,
    date_collecte: str,
) -> list[dict[str, Any]]:
    """Extrait la liste des hôtels d'une page de résultats Booking.

    Cette fonction est pure : elle ne fait aucun appel réseau et peut
    être testée unitairement avec du HTML synthétique.
    """
    hotels: list[dict[str, Any]] = []

    # 1. Tentative d'extraction via le JSON embarqué dans la page.
    marqueur = '"results":[{"__typename":"SearchResultProperty"'
    debut_json = html.find(marqueur)
    if debut_json != -1:
        # Le marqueur commence par la clé « "results": » : le tableau suit immédiatement.
        idx = debut_json + len('"results":')
        decodeur = json.JSONDecoder()
        try:
            resultats, _ = decodeur.raw_decode(html, idx)
        except json.JSONDecodeError:
            resultats = []

        for r in resultats:
            if not isinstance(r, dict):
                continue

            basic = r.get("basicPropertyData") or {}
            if basic.get("isTestProperty"):
                continue
            if r.get("hostTraderLabel") == "PRIVATE":
                continue

            location = basic.get("location") or {}
            reviews = basic.get("reviews") or {}
            star = basic.get("starRating") or {}
            display = r.get("displayName") or {}
            description = r.get("description") or {}

            note = None
            if reviews.get("showScore") and "totalScore" in reviews:
                note = reviews["totalScore"]

            hotels.append(
                {
                    "hotel_id": basic.get("id"),
                    "nom": display.get("text"),
                    "url": _construire_url(basic),
                    "latitude": location.get("latitude"),
                    "longitude": location.get("longitude"),
                    "adresse": location.get("address"),
                    "note": note,
                    "nb_avis": reviews.get("reviewsCount"),
                    "etoiles": star.get("value"),  # star vaut {} sans classe
                    "description": description.get("text"),
                    "id_ville": id_ville,
                    "ville": ville,
                    "date_collecte": date_collecte,
                    "source": "booking",
                }
            )
        return hotels

    # 2. Repli BeautifulSoup si le JSON embarqué est introuvable.
    soupe = BeautifulSoup(html, "html.parser")
    for carte in soupe.select("div[data-testid=property-card]"):
        lien_tag = carte.select_one("[data-testid=title-link]")
        titre_tag = carte.select_one("[data-testid=title]")
        note_tag = carte.select_one("[data-testid=review-score]")

        titre = titre_tag.get_text(strip=True) if titre_tag else None
        href = lien_tag.get("href") if lien_tag else None
        url = _canoniser_url(href) if isinstance(href, str) and href else None

        note = None
        if note_tag:
            texte_note = note_tag.get_text(strip=True)
            match = re.search(r"(\d+[.,]?\d*)", texte_note)
            if match:
                note = float(match.group(1).replace(",", "."))

        hotels.append(
            {
                "hotel_id": None,
                "nom": titre,
                "url": url,
                "latitude": None,
                "longitude": None,
                "adresse": None,
                "note": note,
                "nb_avis": None,
                "etoiles": None,
                "description": None,
                "id_ville": id_ville,
                "ville": ville,
                "date_collecte": date_collecte,
                "source": "booking",
            }
        )

    return hotels


def _construire_url(basic: dict[str, Any]) -> str | None:
    """Construit l'URL canonique d'un hôtel à partir du JSON embarqué."""
    location = basic.get("location") or {}
    pays = location.get("countryCode")
    page = basic.get("pageName")
    if not pays or not page:
        return None
    return f"https://www.booking.com/hotel/{pays}/{page}.fr.html"


def _canoniser_url(url: str) -> str:
    """Retire les paramètres de suivi d'une URL Booking."""
    return url.split("?")[0].split("#")[0]


def url_recherche(ville: str) -> str:
    """URL de la page de résultats d'une ville (filtre « hôtels », une seule page)."""
    return URL_RECHERCHE + "?ss=" + quote(ville + ", France") + "&lang=fr&nflt=ht_id%3D204"


class BookingHotelsSpider(scrapy.Spider):
    """Spider Scrapy pour collecter les hôtels d'une liste de villes."""

    name = "booking_hotels"

    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "USER_AGENT": config.USER_AGENT,
        # Délai nominal entre deux requêtes : 3 secondes.
        "DOWNLOAD_DELAY": 3,
        # Aléa autour de ce délai : ±50 %, soit un délai tiré entre 1,5 et 4,5 s, pour un rythme
        # humain et jamais en rafale. DOWNLOAD_DELAY_JITTER existe depuis Scrapy 2.19 (version
        # figée dans requirements.txt) et remplace RANDOMIZE_DOWNLOAD_DELAY, désormais déprécié :
        # avant la 2.19, c'est ce dernier (True par défaut) qui produisait le même aléa. Le test
        # test_reglages_scrapy_connus vérifie que chaque réglage de cette liste existe vraiment.
        "DOWNLOAD_DELAY_JITTER": 0.5,
        "CONCURRENT_REQUESTS": 1,
        # AutoThrottle ralentit encore le robot si le serveur répond lentement.
        "AUTOTHROTTLE_ENABLED": True,
        "DEFAULT_REQUEST_HEADERS": {"Accept-Language": "fr-FR"},
        "LOG_LEVEL": "INFO",
    }

    def __init__(self, villes: list[dict[str, Any]], sortie: str, **kwargs: Any) -> None:
        """Initialise le spider avec la liste des villes et le fichier de sortie.

        Parameters
        ----------
        villes : list[dict]
            Chaque dict contient id_ville, ville et date_collecte.
        sortie : str
            Chemin du fichier JSON lines temporaire où écrire les items.
        """
        super().__init__(**kwargs)
        self.villes = villes
        self.sortie = sortie
        # Rapport de collecte, ville par ville : statut HTTP, issue, taille de la réponse...
        self.rapport: dict[str, dict[str, Any]] = {}

    async def start(self) -> AsyncIterator[scrapy.Request]:
        """Requêtes initiales (API de Scrapy 2.13 et suivantes, qui n'appelle plus
        start_requests) ; on délègue à start_requests pour rester compatible avec les versions
        antérieures."""
        for requete in self.start_requests():
            yield requete

    def start_requests(self) -> Iterator[scrapy.Request]:
        """Génère les requêtes pour chaque ville (une seule page)."""
        for v in self.villes:
            ville = v["ville"]
            yield scrapy.Request(
                url_recherche(ville),
                callback=self.parse,
                errback=self.echec,
                meta={
                    "id_ville": v["id_ville"],
                    "ville": ville,
                    "date_collecte": v["date_collecte"],
                    # Par défaut Scrapy écarte les réponses 4xx/5xx avant parse() : le refus
                    # (403, 429, 503...) disparaîtrait alors du rapport. On les laisse toutes
                    # passer pour consigner le statut HTTP de chaque ville.
                    "handle_httpstatus_all": True,
                },
            )

    def parse(self, response: Response, **kwargs: Any) -> Iterator[dict[str, Any]]:
        """Analyse une page de résultats ; s'arrête net si le site oppose un contrôle anti-robot.

        Booking répond parfois par un défi AWS WAF (statut 202, page de quelques Ko contenant
        un script « challenge ») au lieu des résultats. Le robot ne cherche PAS à le résoudre ni
        à le contourner : il consigne la ville comme « bloquée » et passe à la suivante.
        """
        id_ville = response.meta["id_ville"]
        ville = response.meta["ville"]
        date_collecte = response.meta["date_collecte"]
        # Ce qui est consigné pour CHAQUE ville, quelle que soit l'issue : le statut HTTP exact,
        # l'URL demandée et la taille de la réponse (un défi anti-robot fait quelques Ko).
        entree = {
            "statut_http": response.status,
            "url": response.url,
            "octets": len(response.body),
        }
        if response.status == 202 or "AwsWafIntegration" in response.text:
            entree["issue"] = "bloquee_par_controle_anti_robot"
            self.rapport[ville] = entree
            self.logger.warning("%s : contrôle anti-robot du site, page non collectée", ville)
            return
        if response.status != 200:
            # Refus ou erreur du serveur : on le consigne et on n'insiste pas.
            entree["issue"] = f"refusee_http_{response.status}"
            self.rapport[ville] = entree
            self.logger.warning("%s : réponse HTTP %s, page non collectée", ville, response.status)
            return
        hotels = extraire_hotels(response.text, id_ville, ville, date_collecte)
        entree["issue"] = "ok" if hotels else "page_sans_hotel"
        entree["nb_hotels"] = len(hotels)
        self.rapport[ville] = entree
        yield from hotels

    def echec(self, failure: Failure) -> None:
        """Erreur avant toute réponse : réseau, délai dépassé, requête écartée par robots.txt.

        Sans ce rattrapage, la ville serait simplement absente du rapport. On la consigne avec
        le type de l'erreur, sans statut HTTP puisqu'il n'y a pas eu de réponse.
        """
        # Scrapy attache la requête à l'échec (attribut non déclaré par Twisted) et un échec
        # porte toujours le type de son exception.
        ville = failure.request.meta["ville"]  # type: ignore[attr-defined]
        nom_erreur = failure.type.__name__  # type: ignore[union-attr]
        # IgnoreRequest est l'exception levée par Scrapy quand robots.txt interdit l'URL.
        issue = "interdite_par_robots_txt" if nom_erreur == "IgnoreRequest" else "erreur_reseau"
        self.rapport[ville] = {"statut_http": None, "issue": issue, "erreur": nom_erreur}
        self.logger.warning("%s : %s (%s)", ville, issue, nom_erreur)

    def closed(self, reason: str) -> None:
        """À la fermeture : écrit le rapport de collecte (statut par ville) à côté des items."""
        for v in self.villes:
            # Une ville ni servie ni en erreur (arrêt prématuré du robot) doit rester visible.
            self.rapport.setdefault(
                v["ville"], {"statut_http": None, "issue": "aucune_reponse", "octets": 0}
            )
        # newline="\n" : le rapport a les mêmes fins de ligne sur tous les systèmes.
        with open(self.sortie + ".rapport.json", "w", encoding="utf-8", newline="\n") as fichier:
            json.dump(
                {"fin": reason, "villes": self.rapport}, fichier, ensure_ascii=False, indent=2
            )


def lancer_collecte_booking(villes_df: pd.DataFrame, lake: DataLake) -> pd.DataFrame:
    """Lance le robot Scrapy dans un processus séparé et archive les hôtels dans le data lake.

    Pourquoi un processus séparé : le moteur réseau de Scrapy (Twisted) ne peut être démarré
    qu'une fois par processus Python. Lancé directement depuis un notebook, le robot
    échouerait à la seconde exécution de la cellule. Le sous-processus lit la liste des villes
    dans un fichier JSON et écrit les hôtels en JSON lines ; ce fichier est ensuite copié tel
    quel dans la zone « raw » du data lake.
    """
    date_jour = config.date_collecte()
    villes = [
        {"id_ville": int(ligne["id_ville"]), "ville": ligne["ville"], "date_collecte": date_jour}
        for _, ligne in villes_df.iterrows()
    ]
    # robots.txt est lu et archivé daté AVANT toute requête de recherche, puis interprété règle
    # par règle pour la première URL réellement demandée : on s'arrête s'il l'interdit.
    robots = archiver_robots(lake, URL_RECHERCHE, source="booking")
    decision = verdict(robots["texte"], config.USER_AGENT, url_recherche(villes[0]["ville"]))
    if not decision["autorise"]:
        raise PermissionError(f"robots.txt interdit la collecte : règle {decision['regle']}")
    with tempfile.TemporaryDirectory() as dossier:
        entree = os.path.join(dossier, "villes.json")
        sortie = os.path.join(dossier, "hotels.jsonl")
        with open(entree, "w", encoding="utf-8") as fichier:
            json.dump(villes, fichier, ensure_ascii=False)
        subprocess.run(
            [sys.executable, "-m", "kayak.collecte.booking_spider", entree, sortie],
            check=True,
            cwd=config.RACINE,
        )
        with open(sortie, "rb") as fichier:
            contenu = fichier.read()
        with open(sortie + ".rapport.json", "rb") as fichier:
            rapport = fichier.read()
    lake.ecrire(f"raw/booking/date={date_jour}/hotels.jsonl", contenu)
    # Le rapport garde la trace des villes bloquées : c'est la preuve que la collecte s'est
    # arrêtée d'elle-même devant le contrôle du site.
    lake.ecrire(f"raw/booking/date={date_jour}/rapport_collecte.json", rapport)
    lignes = [json.loads(ligne) for ligne in contenu.decode("utf-8").splitlines() if ligne]
    return pd.DataFrame(lignes)


def _executer_robot(chemin_villes: str, chemin_sortie: str) -> None:
    """Point d'entrée du sous-processus : exécute le robot et écrit les hôtels collectés."""
    with open(chemin_villes, encoding="utf-8") as fichier:
        villes = json.load(fichier)
    processus = CrawlerProcess(
        settings={
            **BookingHotelsSpider.custom_settings,
            "FEEDS": {
                chemin_sortie: {
                    "format": "jsonlines",
                    "overwrite": True,
                    "store_empty": True,
                    "encoding": "utf8",
                }
            },
        }
    )
    processus.crawl(BookingHotelsSpider, villes=villes, sortie=chemin_sortie)
    processus.start()


if __name__ == "__main__":
    _executer_robot(sys.argv[1], sys.argv[2])
