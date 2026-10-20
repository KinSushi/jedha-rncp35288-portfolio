# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Minimisation des données OpenStreetMap : ni courriel ni téléphone dans le data lake.

Pourquoi ce module existe : les réponses de l'API Overpass portent toutes les étiquettes de
chaque établissement, dont ses moyens de contact (``email``, ``phone``, ``contact:*``...). Aucun
résultat du projet ne s'en sert : la conversion (``kayak/collecte/hotels_osm.py``, ``convertir``)
ne lit que le nom, le site web, l'adresse, la commune, le pays, les étoiles et la description.
Les garder dans la zone brute, c'est conserver des données dont on n'a pas besoin, parfois des
adresses ou des numéros de personnes (chambres d'hôtes, courriels grand public). Ce module les
retire, et masque les courriels et les numéros qui apparaissent dans un texte libre.

Deux usages :
- **à la collecte** : ``hotels_osm.interroger`` minimise chaque réponse dès sa réception, avant
  tout archivage ;
- **hors ligne** (aucun appel réseau), pour les archives écrites avant cette règle :
      python -m kayak.collecte.minimisation --date 2026-10-11

CHOIX
-----
- **Retenu : retirer les clés de contact** (``email``, ``phone``, ``mobile``, ``fax`` et toute clé
  ``contact:...``), **sauf ``contact:website``**, que ``convertir`` lit pour l'adresse du site de
  l'hôtel (sans elle, l'URL de certains hôtels changerait). *Écarté* : une liste blanche des seules
  clés utiles, qui ferait perdre le reste de la réponse brute (identifiants, références) et donc
  la possibilité de rejouer une conversion enrichie plus tard.
- **Retenu : masquer, dans toute valeur texte qui reste, ce qui a la forme d'un courriel ou d'un
  numéro de téléphone** (cas réel : la description d'un hôtel de Paris citait deux numéros).
  *Écarté* : retirer la description entière, qui est affichée et ne contient presque jamais de
  numéro.
- **Retenu : des motifs de téléphone stricts** (« + » ou « 00 » suivi d'un indicatif, ou un
  numéro français de 10 chiffres commençant par 0). *Écarté* : « toute suite de 9 chiffres ou
  plus », qui masquait aussi les numéros SIRET, SIREN et TVA des établissements.
- **Retenu : une copie, jamais une modification sur place** : la réponse reçue reste intacte (les
  tests le vérifient), ce qui évite un effet de bord dans ``interroger``.
- **Retenu : réécrire une archive seulement si elle change** : une seconde exécution ne touche à
  rien.

OÙ
---
- **Place** : étape ``osm`` de la collecte, entre la réponse d'Overpass et l'archive
  (``kayak/collecte/hotels_osm.py``, fonction ``interroger``) ; outil hors ligne pour la zone
  ``raw/osm/`` du data lake.
- **Appelants** : ``kayak.collecte.hotels_osm.interroger`` ; la ligne de commande.
  **Dépendances** : bibliothèque standard, ``kayak.config``, ``kayak.stockage.data_lake``.

COMMENT
-------
- ``masquer_coordonnees`` remplace chaque courriel par « [courriel retiré] » et chaque numéro par
  « [téléphone retiré] » ; ``est_cle_de_contact`` dit si une clé est un moyen de contact ;
  ``minimiser_champs`` rend une copie sans clé de contact et aux textes masqués ;
  ``minimiser_reponse`` l'applique aux étiquettes de chaque élément d'une réponse Overpass ;
  ``minimiser_archives`` réécrit les ``ville_NN.json`` et ``hotels.jsonl`` d'une date.
- **Preuves directes** : les clés de contact retirées et ``contact:website`` gardé
  (tests/test_minimisation.py:100) ; les deux numéros d'une description masqués
  (tests/test_minimisation.py:45) ; les archives d'une date réécrites sans contact, avec le bilan
  exact (tests/test_minimisation.py:248).
- **Refus** (tests inverses) : un SIRET, un SIREN, une date ou un identifiant ne sont pas masqués
  (tests/test_minimisation.py:92) ; la réponse reçue n'est pas modifiée
  (tests/test_minimisation.py:194) ; une date sans archive lève une erreur et n'écrit rien
  (tests/test_minimisation.py:299) ; une date absente ou mal formée est refusée par la ligne de
  commande (tests/test_minimisation.py:350) ; une seconde exécution ne réécrit rien
  (tests/test_minimisation.py:284).

POURQUOI
--------
- Référentiel RNCP35288, bloc 1, compétence C1.3 : collecter « dans le respect des normes de
  protection des données utilisateurs définies dans le RGPD ». Principe de minimisation (RGPD,
  article 5, paragraphe 1, c) du règlement (UE) 2016/679.
- Ce qu'il y avait dans les archives avant la minimisation, et ce qui a été retiré : comptes
  écrits dans le README (« Conformité de la collecte »).
- Motifs de téléphone : choix du projet, essayés sur les archives réelles (aucun SIRET, SIREN,
  numéro de TVA, identifiant Wikidata ou adresse web touché) ; source externe : inconnue.

JOURNALISATION
--------------
- Logger ``kayak.collecte.minimisation``. Au niveau INFO : chaque archive réécrite, avec le nombre
  d'étiquettes retirées et de textes masqués. Aucune valeur retirée n'est journalisée (ce serait
  la recopier ailleurs).
- En ligne de commande : INFO via ``logging.basicConfig``, et un bilan imprimé (archives lues,
  réécrites, étiquettes retirées, textes masqués).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from kayak import config
from kayak.stockage.data_lake import DataLake

journal = logging.getLogger(__name__)

CLES_CONTACT = frozenset({"email", "phone", "mobile", "fax"})
PREFIXE_CONTACT = "contact:"
CLE_SITE_GARDEE = "contact:website"  # lue par hotels_osm.convertir (adresse du site de l'hôtel)
MASQUE_COURRIEL = "[courriel retiré]"
MASQUE_TELEPHONE = "[téléphone retiré]"
MOTIF_COURRIEL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Un numéro international (« +33 1 xx xx xx xx », « +331xxxxxxxx », « 0033 (0)1 ... ») ou un
# numéro français à 10 chiffres (« 0x xx xx xx xx », « 0x.xx.xx.xx.xx »). Les séparateurs permis
# sont l'espace, le point et le tiret ; un SIRET (14 chiffres collés) ne correspond pas.
MOTIF_TELEPHONE = re.compile(
    r"(?:\+|\b00)\d{2,3}(?:[ .-]?\(0\))?(?:[ .-]?\d){8,10}\b"
    r"|\b0[1-9](?:[ .-]?\d{2}){4}\b"
)
MOTIF_ARCHIVE_VILLE = re.compile(r"ville_\d+\.json")
MOTIF_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def masquer_coordonnees(texte: str) -> str:
    """Remplace chaque courriel et chaque numéro de téléphone du texte par un masque."""
    sans_courriel = MOTIF_COURRIEL.sub(MASQUE_COURRIEL, texte)
    return MOTIF_TELEPHONE.sub(MASQUE_TELEPHONE, sans_courriel)


def est_cle_de_contact(cle: str) -> bool:
    """Vrai pour une clé qui porte un moyen de contact (``contact:website`` excepté)."""
    if cle == CLE_SITE_GARDEE:
        return False
    return cle in CLES_CONTACT or cle.startswith(PREFIXE_CONTACT)


def minimiser_champs(champs: dict[str, Any]) -> dict[str, Any]:
    """Copie sans clé de contact, où chaque texte a ses courriels et numéros masqués."""
    return {
        cle: masquer_coordonnees(valeur) if isinstance(valeur, str) else valeur
        for cle, valeur in champs.items()
        if not est_cle_de_contact(cle)
    }


def minimiser_reponse(reponse: dict[str, Any]) -> dict[str, Any]:
    """Copie d'une réponse Overpass dont chaque élément a des étiquettes minimisées.

    Les autres clés (marque de la requête, remarque, version) et l'ordre des éléments sont
    gardés ; un élément sans étiquettes est repris tel quel. La réponse reçue n'est pas modifiée.
    """
    copie = dict(reponse)
    if "elements" in copie:
        copie["elements"] = [
            {**element, "tags": minimiser_champs(element["tags"])}
            if "tags" in element
            else element
            for element in copie["elements"]
        ]
    return copie


def _compter_retraits(avant: dict[str, Any], apres: dict[str, Any]) -> tuple[int, int]:
    """(étiquettes retirées, textes masqués) entre deux versions d'un même ensemble de champs."""
    retirees = len(avant) - len(apres)
    masques = sum(1 for cle, valeur in apres.items() if valeur != avant[cle])
    return retirees, masques


def _minimiser_archive_ville(texte: str) -> tuple[str, int, int]:
    """Réponse Overpass archivée → (texte minimisé, étiquettes retirées, textes masqués)."""
    reponse = json.loads(texte)
    minimisee = minimiser_reponse(reponse)
    retirees = masques = 0
    # Une archive sans liste « elements » n'est pas une réponse Overpass : KeyError, pas d'oubli.
    for position, avant in enumerate(reponse["elements"]):
        if "tags" in avant:
            r, m = _compter_retraits(avant["tags"], minimisee["elements"][position]["tags"])
            retirees, masques = retirees + r, masques + m
    return json.dumps(minimisee, ensure_ascii=False), retirees, masques


def _minimiser_hotels_jsonl(texte: str) -> tuple[str, int, int]:
    """``hotels.jsonl`` (une ligne JSON par hôtel) → (texte minimisé, retirées, masqués)."""
    lignes = []
    retirees = masques = 0
    for ligne in texte.splitlines():
        hotel = json.loads(ligne)
        minimise = minimiser_champs(hotel)
        r, m = _compter_retraits(hotel, minimise)
        retirees, masques = retirees + r, masques + m
        lignes.append(json.dumps(minimise, ensure_ascii=False) + "\n")
    return "".join(lignes), retirees, masques


def minimiser_archives(lake: DataLake) -> dict[str, int]:
    """Minimise, sans aucun réseau, les archives OpenStreetMap de la date de collecte.

    Lit chaque ``raw/osm/date=<date>/ville_NN.json`` et le ``hotels.jsonl`` de la date, et ne
    réécrit que ceux qui changent. Lève ``FileNotFoundError`` si la date n'a aucune archive (une
    purge qui ne trouve rien est une erreur de date, pas un succès).
    """
    prefixe = f"raw/osm/date={config.date_collecte()}/"
    bilan = {"archives_lues": 0, "archives_reecrites": 0, "etiquettes_retirees": 0,
             "textes_masques": 0}
    for chemin in sorted(lake.lister(prefixe)):
        nom = PurePosixPath(chemin).name
        if MOTIF_ARCHIVE_VILLE.fullmatch(nom):
            traiter = _minimiser_archive_ville
        elif nom == "hotels.jsonl":
            traiter = _minimiser_hotels_jsonl
        else:
            continue
        texte = lake.lire(chemin).decode()  # UTF-8, encodage par défaut de bytes.decode
        nouveau, retirees, masques = traiter(texte)
        bilan["archives_lues"] += 1
        if nouveau != texte:
            lake.ecrire(chemin, nouveau)
            bilan["archives_reecrites"] += 1
            bilan["etiquettes_retirees"] += retirees
            bilan["textes_masques"] += masques
            journal.info(
                "%s : %d étiquette(s) de contact retirée(s), %d texte(s) masqué(s)",
                chemin, retirees, masques,
            )
    if bilan["archives_lues"] == 0:
        raise FileNotFoundError(f"aucune archive OpenStreetMap sous {prefixe}")
    return bilan


def _date_valide(texte: str) -> str:
    """Type argparse : une date AAAA-MM-JJ réelle, sinon refus (code de sortie 2).

    Le motif écarte les autres écritures qu'accepte ``date.fromisoformat`` (« 20261011 ») : le
    data lake range les collectes sous ``date=AAAA-MM-JJ``.
    """
    if not MOTIF_DATE.fullmatch(texte):
        raise argparse.ArgumentTypeError(f"date mal écrite (AAAA-MM-JJ attendu) : {texte!r}")
    try:
        date.fromisoformat(texte)
    except ValueError as erreur:
        raise argparse.ArgumentTypeError(f"date impossible : {texte!r}") from erreur
    return texte


def main() -> None:
    """Point d'entrée en ligne de commande : minimisation hors ligne d'une collecte archivée."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    analyseur = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    analyseur.add_argument("--date", required=True, type=_date_valide,
                           help="date de collecte AAAA-MM-JJ")
    arguments = analyseur.parse_args()
    os.environ["KAYAK_DATE_COLLECTE"] = arguments.date
    bilan = minimiser_archives(DataLake())
    print(
        f"{bilan['archives_lues']} archives lues, {bilan['archives_reecrites']} réécrites : "
        f"{bilan['etiquettes_retirees']} étiquettes de contact retirées, "
        f"{bilan['textes_masques']} textes masqués (aucun appel réseau)"
    )


if __name__ == "__main__":
    main()
