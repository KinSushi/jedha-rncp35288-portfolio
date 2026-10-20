# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Orchestration du projet Kayak : collecte → data lake → ETL Spark → entrepôt → cartes.

Chaque étape lit ses entrées dans le data lake et y écrit ses sorties : on peut donc relancer
une seule étape (``--etapes etl cartes``) sans refaire la collecte, et relancer tout le pipeline
sans doublon (étapes idempotentes, collectes rangées par date).

Usage :
    python -m kayak.pipeline                         # toutes les étapes, date du jour
    python -m kayak.pipeline --etapes etl cartes     # ETL et cartes seulement
    python -m kayak.pipeline --date 2026-10-10       # rejouer une collecte archivée

CHOIX
-----
- **Retenu : des étapes qui communiquent par le data lake**, chacune relisant ses entrées.
  *Écarté* : se passer les données en mémoire, qui obligerait à tout refaire pour relancer une
  seule étape (par exemple les cartes après une correction).
- **Retenu : les étapes ``etl`` et ``cartes`` ne collectent rien** et se rejouent sur le lac local
  avec ``--date`` : c'est le mode hors ligne du projet. *Écarté* : relancer la collecte pour
  recalculer.
- **Retenu : les cartes lisent TOUJOURS la zone curated** (Parquet écrit par l'ETL), avec les
  vraies coordonnées des villes. *Écarté* : reconstituer les villes à partir du premier hôtel de
  chaque ville, qui plaçait les destinations aux coordonnées d'un hôtel.
- **Retenu : importer chaque étape à la demande** (imports dans le corps de la fonction), ce qui
  évite de charger Spark ou Scrapy pour une relance partielle.
- **Retenu : journaliser toute requête HTTP des étapes de collecte** (``journaliser_requetes``).

OÙ
---
- **Place** : point d'entrée en ligne de commande (``python -m kayak.pipeline``) et fonction
  ``executer`` appelée par les tests. **Appelle** les modules de ``kayak/collecte``,
  ``kayak/etl``, ``kayak/cartes.py`` et ``kayak/stockage/data_lake.py``.
- **Dépendances** : pandas (lecture des tables), ``kayak.config`` (date), ``argparse``.

COMMENT
-------
- ``executer`` fixe la date de collecte dans l'environnement, ouvre le lake, ouvre le journal HTTP
  si une étape de collecte est demandée, puis ``_executer_etapes`` enchaîne les étapes demandées
  dans l'ordre de ``ETAPES`` et range leurs résultats ; ``lire_parquet`` lit une table Parquet
  écrite par Spark (un dossier de parties) avec un filtre appliqué dès la lecture ; ``main`` lit
  la ligne de commande.
- **Preuves** : les parties Parquet sont assemblées dans l'ordre (tests/test_pipeline.py:28) ;
  l'étape cartes seule utilise les coordonnées des villes (tests/test_pipeline.py:57) ; chaque
  étape est appelée avec ses entrées (tests/test_pipeline_inverses.py:643).
- **Refus** (tests inverses) : une table absente est signalée (tests/test_pipeline.py:50) ;
  l'exécution s'arrête à la première étape en échec (tests/test_pipeline_inverses.py:364) ; une
  étape cartes en échec n'enregistre aucune carte (tests/test_pipeline_inverses.py:427).

POURQUOI
--------
- Le défaut des cartes reconstruites à partir d'un hôtel est décrit dans l'en-tête de
  tests/test_pipeline.py et corrigé dans kayak/pipeline.py:183.
- La collecte ne doit pas être relancée pour rejouer l'ETL : Booking.com a opposé un contrôle
  anti-robot et les autres services répondent différemment d'un jour à l'autre (notebook, sections
  2 et 3).
- Choix d'architecture « le lake est le point de passage » : étude d'architecture du projet ;
  source : inconnue pour un texte de référence externe.

JOURNALISATION
--------------
- Logger ``kayak`` : un message INFO au début de ``executer`` (« Date de collecte : <date> ; data
  lake : <mode> », kayak/pipeline.py:121) et un message INFO « Villes sans hôtel Booking : <n> ;
  repli OpenStreetMap » pour l'étape ``osm``.
- ``main`` configure ``logging.basicConfig`` au niveau INFO (heure, niveau, message) ; il imprime
  aussi les contrôles de qualité et les lignes chargées quand l'ETL a tourné.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import os
from typing import Any

import pandas as pd

from kayak import config
from kayak.collecte.journal import journaliser_requetes
from kayak.stockage.data_lake import DataLake

ETAPES = ["destinations", "geocodage", "meteo", "booking", "osm", "etl", "cartes"]
# Étapes qui interrogent des services web : leurs requêtes HTTP sont journalisées.
ETAPES_DE_COLLECTE = {"destinations", "geocodage", "meteo", "booking", "osm"}
journal = logging.getLogger("kayak")


def lire_csv(lake: DataLake, chemin: str) -> pd.DataFrame:
    """Lit un CSV du data lake (local ou S3) dans un DataFrame pandas."""
    return pd.read_csv(io.BytesIO(lake.lire(chemin)))


def lire_parquet(
    lake: DataLake, dossier: str, filtres: list[tuple[str, str, Any]] | None = None
) -> pd.DataFrame:
    """Lit une table Parquet du data lake (un dossier de fichiers ``part-*.parquet``).

    Spark écrit une table sous la forme d'un dossier ; on lit chaque fichier par ``lake.lire``
    (donc aussi bien en local que sur S3) et on les assemble dans l'ordre des noms, qui est
    l'ordre d'écriture. ``filtres`` (format de pyarrow, par exemple
    ``[("rang_meteo", "<=", 5)]``) est appliqué dès la lecture : seules les lignes utiles
    arrivent dans pandas, au lieu de toute la table.
    """
    parties = sorted(c for c in lake.lister(dossier) if c.endswith(".parquet"))
    if not parties:
        raise FileNotFoundError(f"aucun fichier Parquet sous {dossier}")
    return pd.concat(
        [pd.read_parquet(io.BytesIO(lake.lire(p)), filters=filtres) for p in parties],
        ignore_index=True,
    )


def executer(etapes: list[str], date: str | None = None) -> dict[str, Any]:
    """Exécute les étapes demandées, dans l'ordre ; renvoie les résultats de chacune."""
    if date:
        # La date fixe le dossier « date=... » de toutes les étapes (config.date_collecte()).
        os.environ["KAYAK_DATE_COLLECTE"] = date
    date = config.date_collecte()
    lake = DataLake()
    journal.info("Date de collecte : %s ; data lake : %s", date, lake.mode)
    resultats: dict[str, Any] = {}
    # Toute requête HTTP d'une étape de collecte est journalisée (statut, taille, durée) dans
    # raw/journal_http/ ; le journal est écrit même si une étape échoue.
    with contextlib.ExitStack() as pile:
        if ETAPES_DE_COLLECTE & set(etapes):
            pile.enter_context(journaliser_requetes(lake))
        _executer_etapes(etapes, lake, date, resultats)
    return resultats


def _executer_etapes(
    etapes: list[str], lake: DataLake, date: str, resultats: dict[str, Any]
) -> None:
    """Corps de ``executer`` : enchaîne les étapes demandées et range leurs résultats."""
    base = f"date={date}"

    if "destinations" in etapes:
        from kayak.collecte.destinations import collecter_destinations

        resultats["destinations"] = collecter_destinations(lake)
    if "geocodage" in etapes:
        from kayak.collecte.geocodage import collecter_coordonnees

        destinations = lire_csv(lake, f"raw/destinations/{base}/destinations.csv")
        resultats["coordonnees"] = collecter_coordonnees(destinations, lake)
    if "meteo" in etapes:
        from kayak.collecte.meteo import collecter_meteo

        coordonnees = lire_csv(lake, f"raw/geocodage/{base}/coordonnees.csv")
        resultats["meteo"] = collecter_meteo(coordonnees, lake)
    if "booking" in etapes:
        from kayak.collecte.booking_spider import lancer_collecte_booking

        destinations = lire_csv(lake, f"raw/destinations/{base}/destinations.csv")
        resultats["hotels"] = lancer_collecte_booking(destinations, lake)
    if "osm" in etapes:
        # Repli : hôtels OpenStreetMap pour les villes que Booking n'a pas servies (contrôle
        # anti-robot du site, voir collecte/hotels_osm.py).
        from kayak.collecte.hotels_osm import collecter_hotels_osm

        coordonnees = lire_csv(lake, f"raw/geocodage/{base}/coordonnees.csv")
        chemin_booking = f"raw/booking/{base}/hotels.jsonl"
        servies = set()
        if lake.existe(chemin_booking) and lake.lire(chemin_booking).strip():
            servies = set(
                pd.read_json(io.BytesIO(lake.lire(chemin_booking)), lines=True)["id_ville"]
            )
        manquantes = coordonnees[~coordonnees["id_ville"].isin(servies)]
        journal.info("Villes sans hôtel Booking : %d ; repli OpenStreetMap", len(manquantes))
        resultats["hotels_osm"] = collecter_hotels_osm(manquantes, lake)
    if "etl" in etapes:
        from kayak.etl.spark_etl import executer_etl

        resultats["etl"] = executer_etl(lake, date, config.DB_URL)
    if "cartes" in etapes:
        from kayak.cartes import carte_destinations, carte_hotels, enregistrer
        from kayak.collecte.hotels_osm import diagnostic_troncature

        # Les cartes se lisent TOUJOURS dans la zone « curated » (Parquet écrit par l'ETL), que
        # l'ETL vienne de tourner ou non : « --etapes cartes » seul fonctionne donc, et la table
        # des villes porte leurs vraies coordonnées (et non celles du premier hôtel venu).
        villes = lire_parquet(lake, f"curated/villes/{base}/villes.parquet")
        # Seuls les hôtels des 5 meilleures destinations servent aux cartes : le filtre est
        # appliqué à la lecture du Parquet, la table entière n'entre pas dans pandas.
        enrichi = lire_parquet(
            lake,
            f"curated/kayak_enrichi/{base}/kayak_enrichi.parquet",
            filtres=[("rang_meteo", "<=", 5)],
        )
        # Si les réponses OpenStreetMap viennent de l'ancienne requête plafonnée, la carte le dit.
        diagnostic = diagnostic_troncature(lake)
        mention = (
            f"20 meilleurs parmi au plus {diagnostic['plafond']} hôtels OSM par ville"
            if diagnostic["villes_plafonnees"]
            else None
        )
        resultats["cartes"] = {
            "destinations": enregistrer(carte_destinations(villes), "top5_destinations"),
            "hotels": enregistrer(
                carte_hotels(enrichi, villes, note_echantillon=mention), "top20_hotels"
            ),
        }


def main() -> None:
    """Point d'entrée en ligne de commande."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    analyseur = argparse.ArgumentParser(description="Pipeline Kayak")
    analyseur.add_argument("--etapes", nargs="+", choices=ETAPES, default=ETAPES)
    analyseur.add_argument("--date", default=None, help="date de collecte AAAA-MM-JJ")
    arguments = analyseur.parse_args()
    resultats = executer(arguments.etapes, arguments.date)
    if "etl" in resultats:
        print("Contrôles de qualité :", resultats["etl"]["qualite"])
        print("Lignes chargées :", resultats["etl"]["charges"])


if __name__ == "__main__":
    main()
