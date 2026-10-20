# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Collecte et réconciliation de la liste des destinations.

La source one-week-in.com publie les 35 villes sous forme de titres h2.
Ces titres doivent être nettoyés (numérotation et parenthèses retirées),
normalisés, puis réconciliés avec la liste de référence du référentiel.

CHOIX
-----
- **Retenu : réconcilier les noms de la page avec la liste de référence de l'énoncé**
  (``config.VILLES_ENONCE``) et garder l'ordre de la référence, qui fixe ``id_ville`` de 1 à 35.
  La page écrit « Verdon Gorge » et la faute « Nimmes » : on ne peut pas s'y fier telle quelle.
- **Retenu : une table d'alias explicite** (``ALIAS``) pour les deux écarts connus, après une
  comparaison sans accents ni casse. *Alternative non retenue* : un appariement approximatif, qui
  pourrait rattacher à tort une ville de la page à une autre ; un écart inconnu est plutôt signalé
  « absente_de_la_source », jugement de conception.
- **Retenu : vérifier robots.txt AVANT de télécharger la page**, l'archiver daté, et s'arrêter
  s'il l'interdit. *Écarté* : télécharger d'abord.
- **Retenu : archiver le HTML brut mais ne pas le livrer** (contenu d'un site tiers) ; seul le CSV
  réconcilié est livré.

OÙ
---
- **Place** : première étape de la collecte (kayak/pipeline.py:141) ; son CSV est lu par le
  géocodage et par la collecte Booking.
- **Appelants** : ``kayak/pipeline.py``. **Dépendances** : ``requests``, BeautifulSoup, pandas,
  ``kayak.collecte.robots``, ``kayak.config``, ``kayak.stockage.data_lake``.

COMMENT
-------
- ``extraire_destinations`` lit les titres ``h2``, retire la numérotation et les parenthèses ;
  ``normaliser`` met un nom sous forme comparable ; ``reconcilier`` produit une ligne par ville de
  référence avec un statut (``identique``, ``alias`` ou ``absente_de_la_source``) ;
  ``collecter_destinations`` enchaîne robots.txt, téléchargement, archives et pause d'une seconde.
- **Preuves directes** : titres nettoyés (tests/test_destinations.py:20) ; alias et absence
  (tests/test_destinations.py:40) ; archives écrites (tests/test_destinations.py:83).
- **Refus** (tests inverses) : un robots.txt qui interdit arrête la collecte sans aucune requête
  (tests/test_destinations.py:69) ; une entrée non valide est refusée
  (tests/test_destinations_inverses.py:410) ; une coupure réseau est propagée sans rien archiver
  (tests/test_destinations_inverses.py:556).

POURQUOI
--------
- Les deux alias viennent de la comparaison de la page avec l'énoncé, visible dans la section 2 du
  notebook (« Verdon Gorge » et « Nimmes », statut ``alias``) ; test de référence :
  tests/test_destinations.py:40.
- L'obligation de lire robots.txt avant la collecte vient de la compétence C1.3 du référentiel
  RNCP35288 (collecte web « dans le respect des normes ») : référentiel de la certification
  (fiche publique RNCP35288).
- Pause d'une seconde entre requêtes : usage poli, choix du projet, source : inconnue.

JOURNALISATION
--------------
- Aucun message ``logging``. La trace est une ARCHIVE du lake : ``robots.txt`` daté (voir
  ``kayak/collecte/robots.py``), ``page.html`` et ``destinations.csv`` ; la requête HTTP est
  consignée par ``kayak/collecte/journal.py``.
- Un refus de robots.txt lève ``PermissionError`` ; un statut HTTP d'erreur lève l'exception de
  ``requests`` (``raise_for_status``).
"""

from __future__ import annotations

import re
import time
import unicodedata

import pandas as pd
import requests
from bs4 import BeautifulSoup

from kayak import config
from kayak.collecte.robots import archiver_robots, verdict
from kayak.stockage.data_lake import DataLake

URL_SOURCE = "https://one-week-in.com/35-cities-to-visit-in-france/"


def extraire_destinations(html: str) -> list[str]:
    """Extrait les noms de villes depuis le HTML de la source.

    Les titres h2 sont de la forme 'N. Nom' ou 'N. Nom (commentaire)'.
    On retire la numérotation et le commentaire entre parenthèses qui termine le titre
    pour ne garder que le nom propre de la destination. Limite connue : une parenthèse
    qui n'est pas fermée en fin de titre n'est pas retirée.
    """
    soupe = BeautifulSoup(html, "html.parser")
    villes: list[str] = []
    for titre in soupe.find_all("h2"):
        texte = titre.get_text(strip=True)
        # Suppression de la numérotation en début de titre.
        sans_numero = re.sub(r"^\d+\.\s*", "", texte)
        # Suppression de tout commentaire entre parenthèses.
        sans_parenthese = re.sub(r"\s*\(.*\)$", "", sans_numero)
        villes.append(sans_parenthese.strip())
    return villes


def normaliser(nom: str) -> str:
    """Normalise un nom pour la comparaison : sans accents, minuscules.

    Seuls les caractères alphabétiques non accentués et les espaces sont conservés : le tiret
    est supprimé (et non remplacé par une espace), donc 'Saint-Malo' devient 'saintmalo' et
    ne se rapproche pas de 'St Malo' ; les ligatures « œ » disparaissent. Les écarts connus
    entre la page source et la référence sont traités par ``ALIAS``.
    """
    nom = nom.lower()
    # Suppression des accents par décomposition Unicode : « è » devient « e » + accent, puis
    # l'accent (caractère combinant) est retiré. « Uzès » donne ainsi « uzes ».
    nom = "".join(c for c in unicodedata.normalize("NFKD", nom) if not unicodedata.combining(c))
    # Conservation des lettres et espaces.
    nom = re.sub(r"[^a-z\s]", "", nom)
    return " ".join(nom.split())


ALIAS = {
    "verdon gorge": "Gorges du Verdon",
    "nimmes": "Nimes",
}


def reconcilier(
    extraites: list[str],
    reference: list[str] = config.VILLES_ENONCE,
) -> pd.DataFrame:
    """Réconcilie les villes extraites avec la liste de référence.

    Pour chaque ville de référence, on cherche un nom identique, un alias
    connu, ou on la marque absente de la source. L'ordre de la référence
    est conservé pour garantir id_ville de 1 à 35.
    """
    index_normalise = {normaliser(v): v for v in extraites}
    lignes = []
    for id_ville, ville_ref in enumerate(reference, start=1):
        cle_ref = normaliser(ville_ref)
        statut = "absente_de_la_source"
        ville_source = None

        # Correspondance exacte sur le nom brut.
        if ville_ref in extraites:
            statut = "identique"
            ville_source = ville_ref
        # Correspondance après normalisation.
        elif cle_ref in index_normalise:
            statut = "identique"
            ville_source = index_normalise[cle_ref]
        # Alias direct : la référence a un alias connu présent dans la source.
        elif cle_ref in ALIAS and normaliser(ALIAS[cle_ref]) in index_normalise:
            statut = "alias"
            ville_source = index_normalise[normaliser(ALIAS[cle_ref])]
        else:
            # Alias inverse : une ville source correspond à un alias pointant
            # vers la référence courante.
            for alias_cle, alias_nom in ALIAS.items():
                if alias_cle in index_normalise and normaliser(alias_nom) == cle_ref:
                    statut = "alias"
                    ville_source = index_normalise[alias_cle]
                    break

        lignes.append(
            {
                "id_ville": id_ville,
                "ville": ville_ref,
                "ville_source": ville_source,
                "statut": statut,
            }
        )
    return pd.DataFrame(lignes)


def collecter_destinations(lake: DataLake) -> pd.DataFrame:
    """Télécharge la page source, vérifie robots.txt, stocke et réconcilie.

    Le HTML brut est archivé dans raw/destinations pour traçabilité, puis
    le CSV réconcilié est également stocké pour les étapes suivantes. Le HTML brut
    (page.html) est le contenu d'un site tiers : il n'est volontairement PAS redistribué
    avec le data lake livré, seul le CSV réconcilié l'est.
    """
    # Vérification du robots.txt avant tout téléchargement : le fichier est lu, archivé daté
    # dans le data lake (preuve de ce que le site déclarait ce jour-là), puis interprété règle
    # par règle (module robots, qui gère les jokers que le module standard ignore).
    robots = archiver_robots(lake, URL_SOURCE, source="destinations")
    if not verdict(robots["texte"], config.USER_AGENT, URL_SOURCE)["autorise"]:
        raise PermissionError(f"robots.txt interdit la collecte sur {URL_SOURCE}")

    date_jour = config.date_collecte()
    reponse = requests.get(URL_SOURCE, headers={"User-Agent": config.USER_AGENT}, timeout=30)
    reponse.raise_for_status()
    html = reponse.text

    # Archivage du HTML brut.
    chemin_html = f"raw/destinations/date={date_jour}/page.html"
    lake.ecrire(chemin_html, html)

    # Extraction et réconciliation.
    villes = extraire_destinations(html)
    df = reconcilier(villes)

    # Archivage du résultat réconcilié.
    chemin_csv = f"raw/destinations/date={date_jour}/destinations.csv"
    lake.ecrire(chemin_csv, df.to_csv(index=False, lineterminator="\n"))

    # Respect d'une pause minimale entre requêtes successives.
    time.sleep(1.0)
    return df
