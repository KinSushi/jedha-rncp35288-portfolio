# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Mesures réelles servant de base à la note de dimensionnement (aucune valeur supposée).

Pourquoi ce module : un dimensionnement honnête part de MESURES (taille d'une ligne, mémoire
consommée, volume de journal de transactions), puis les extrapole avec des hypothèses déclarées.
Ce module produit les mesures, afin que quiconque puisse les refaire :

- ``postgres`` : charge les données synthétiques dans un vrai PostgreSQL (schéma déjà appliqué,
  base vide) et relève, table par table, la taille du tas, des index et le volume de journal de
  transactions (WAL) produit ;
- ``demo`` : exécute la démonstration autonome en échantillonnant la mémoire du processus et
  relève la taille des fichiers produits.

Usage (depuis stripe/) :
    python -m stripe_demo.mesures postgres --url postgresql+psycopg://utilisateur@hote:5432/base
    python -m stripe_demo.mesures demo
Le résultat est écrit en JSON dans sorties/mesures_<commande>.json.

CHOIX
-----
- Retenu : mesurer une fois, publier les fichiers JSON, et laisser `dimensionnement.py` calculer
  à partir d'eux. Écarté : estimer les tailles par le calcul (la taille réelle d'une ligne dépend
  du moteur : tas, index, remplissage).
- Retenu : charger les MÊMES données synthétiques que la démonstration (mêmes fonctions de
  `oltp.py`), pour que les mesures décrivent la charge qu'on prétend dimensionner.
- Retenu : `VACUUM (ANALYZE)` avant de lire les tailles, en validation automatique (la commande
  refuse de tourner dans une transaction), pour mesurer des tailles stabilisées.
- Retenu : un échantillonneur de mémoire dans un fil daemon (une lecture toutes les 20 ms) pour
  trouver le pic ; écarté : une seule lecture finale, qui manquerait le pic.

OÙ
---
- Place : couche « mesures » en amont de `stripe_demo/dimensionnement.py`. Appelants : la
  commande `python -m stripe_demo.mesures` et les tests `tests/test_mesures.py`.
- Dépendances : SQLAlchemy et `psycopg` (PostgreSQL réel pour `postgres`), DuckDB, pandas,
  `psutil`, `stripe_demo/pipeline.py`, `fraude.py`, `oltp.py`.

COMMENT
-------
- `mesurer_postgres` charge `jours` x `par_jour` paiements, lit la position du journal avant et
  après, puis, table par table, le nombre de lignes et les tailles du tas, des index et du
  total ; `mesurer_demo` exécute la démonstration en échantillonnant la mémoire ;
  `mesures_complementaires` rejoue l'état d'entraînement (les `faits_batch` premiers faits),
  mesure la taille du fichier Parquet, la durée d'entraînement, la qualité des alertes du flux et
  la taille d'un message JSON ; `alertes_sur_le_flux` compte alertes, fraudes, vrais positifs,
  précision et rappel au seuil d'alerte.
- Ce qui le prouve (`tests/test_mesures.py`) : un faux moteur PostgreSQL qui répond à chaque
  requête exacte par une valeur connue (toute requête inattendue fait échouer le test), un faux
  processus pour la mémoire, une exécution en petit de la démonstration pour les mesures
  complémentaires, et des cas limites sans alerte ni fraude.

POURQUOI
--------
- Mesures d'occupation disque dans PostgreSQL et de la démonstration :
  `livrables/aia2/09_dimensionnement_finops_greenops.md:7-37`.
- Reproductibilité du modèle reconstitué (ROC-AUC 0,872, PR-AUC 0,16) :
  `livrables/aia2/07_integration_ml.md:73`.
- Valeurs publiées : `livrables/aia2/donnees/mesures_postgres.json:1-101` et
  `livrables/aia2/donnees/mesures_demo.json:1-33`.

JOURNALISATION
--------------
- Pas de module `logging` : la commande écrit le résultat en JSON dans `sorties` et l'affiche
  sur la sortie standard. Une erreur de connexion ou de requête se propage sans être avalée.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sqlalchemy import create_engine, text

from stripe_demo import fraude, oltp, pipeline

SORTIES = Path(__file__).resolve().parent.parent / "sorties"


def mesurer_postgres(url: str, jours: int = 60, par_jour: int = 800) -> dict[str, Any]:
    """Charge `jours` × `par_jour` paiements dans PostgreSQL et mesure l'occupation disque."""
    moteur = create_engine(url)
    with moteur.connect() as cx:
        lsn_debut = cx.execute(text("SELECT pg_current_wal_lsn()")).scalar_one()
    t0 = time.perf_counter()
    oltp.referentiels(moteur, jours=jours + 2)
    debut = datetime(2026, 8, 1, tzinfo=UTC)
    dernier = 0
    for j in range(jours):
        dernier = oltp.paiements_synthetiques(moteur, debut + timedelta(days=j), par_jour,
                                              graine=1000 + j, premier_id=dernier + 1)
    duree = time.perf_counter() - t0
    # VACUUM ne s'exécute pas dans une transaction : connexion en validation automatique.
    with moteur.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
        cx.execute(text("VACUUM (ANALYZE)"))
    with moteur.connect() as cx:
        wal = cx.execute(text("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), :d)"),
                         {"d": lsn_debut}).scalar_one()
        tables = [r[0] for r in cx.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"))]
        mesures = {}
        for table in tables:
            lignes = cx.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            tas, index, total = cx.execute(text(
                "SELECT pg_relation_size(CAST(:t AS regclass)), "
                "pg_indexes_size(CAST(:t AS regclass)), "
                "pg_total_relation_size(CAST(:t AS regclass))"),
                {"t": table}).one()
            mesures[table] = {"lignes": lignes, "tas_octets": int(tas),
                              "index_octets": int(index), "total_octets": int(total),
                              "octets_par_ligne": round(total / lignes, 1) if lignes else None}
        version = cx.execute(text("SHOW server_version")).scalar_one()
    return {"serveur": version, "paiements": dernier, "jours": jours, "duree_chargement_s":
            round(duree, 1), "wal_octets": int(wal), "tables": mesures}


def mesurer_demo() -> dict[str, Any]:
    """Exécute la démonstration autonome et relève pic de mémoire, durée et tailles de fichiers."""
    import psutil

    processus = psutil.Process()
    pic = {"rss": processus.memory_info().rss}
    arret = threading.Event()

    def echantillonner() -> None:
        while not arret.is_set():
            pic["rss"] = max(pic["rss"], processus.memory_info().rss)
            time.sleep(0.02)

    fil = threading.Thread(target=echantillonner, daemon=True)
    fil.start()
    t0 = time.perf_counter()
    rapport = pipeline.executer()
    duree = time.perf_counter() - t0
    arret.set()
    fil.join()
    fichiers = {nom: (pipeline.SORTIES / nom).stat().st_size
                for nom in ("oltp.sqlite", "olap.duckdb")}
    complements = mesures_complementaires(rapport["batch"]["faits"])
    return {"duree_s": round(duree, 1), "pic_rss_octets": pic["rss"],
            "fichiers_octets": fichiers, "paiements": rapport["oltp"]["paiements"],
            "faits": rapport["batch"]["faits"], "latence_mediane_ms":
            rapport["flux"]["latence_mediane_ms"], **complements}


def alertes_sur_le_flux(modele: HistGradientBoostingClassifier, donnees: pd.DataFrame,
                         seuil: float = pipeline.SEUIL_ALERTE) -> dict[str, Any]:
    """Qualité des alertes sur les paiements du flux (étiquette = paiement contesté).

    Mêmes variables et même seuil que le service de score de `pipeline.py`. La « fraude » est
    ici l'étiquette de la simulation : une précision de 100 % n'aurait aucun sens sans cela.
    """
    scores = modele.predict_proba(donnees[fraude.VARIABLES])[:, 1]
    alerte = scores >= seuil
    vrais = int((alerte & (donnees["fraude"].to_numpy() == 1)).sum())
    fraudes = int(donnees["fraude"].sum())
    return {"seuil": seuil, "paiements": len(donnees), "alertes": int(alerte.sum()),
            "fraudes": fraudes, "vrais_positifs": vrais,
            "precision": round(vrais / max(int(alerte.sum()), 1), 3),
            "rappel": round(vrais / max(fraudes, 1), 3)}


def mesures_complementaires(faits_batch: int) -> dict[str, Any]:
    """Taille Parquet des faits, durée d'entraînement, prévalence de test et taille d'un message.

    Le modèle de la démonstration est entraîné sur les `faits_batch` premiers faits (avant le
    flux CDC) : on reconstitue donc exactement cet état dans une base en mémoire, ce qui permet
    aussi de vérifier que les mesures du modèle sont reproductibles.
    """
    source = duckdb.connect(str(pipeline.SORTIES / "olap.duckdb"), read_only=True)
    parquet = pipeline.SORTIES / "faits_paiements.parquet"
    source.execute(f"COPY (SELECT * FROM faits_paiements) TO '{parquet.as_posix()}' "
                   "(FORMAT PARQUET)")
    faits = source.execute("SELECT count(*) FROM faits_paiements").fetchall()[0][0]
    memoire = duckdb.connect(":memory:")
    flux = duckdb.connect(":memory:")
    for table, filtre in (("faits_paiements", f"WHERE id_paiement <= {faits_batch}"),
                          ("dim_client", ""), ("dim_date", "")):
        memoire.register(table, source.execute(f"SELECT * FROM {table} {filtre}").df())
    for table, filtre in (("faits_paiements", f"WHERE id_paiement > {faits_batch}"),
                          ("dim_client", ""), ("dim_date", "")):
        flux.register(table, source.execute(f"SELECT * FROM {table} {filtre}").df())
    source.close()
    t0 = time.perf_counter()
    modele, mesures_modele, _ = fraude.entrainer(memoire)
    entrainement = time.perf_counter() - t0
    alertes = alertes_sur_le_flux(modele, flux.execute(fraude.REQUETE_ENTRAINEMENT).df())
    flux.close()
    # Même découpage temporel que fraude.entrainer : 80 % des lignes (par date) pour apprendre.
    donnees = memoire.execute(fraude.REQUETE_ENTRAINEMENT).df().sort_values("date")
    test = donnees[donnees["date"] > donnees["date"].quantile(0.8)]
    prevalence_test = float(test["fraude"].mean())
    memoire.close()
    # Taille d'un message JSON d'un paiement (colonnes de l'OLTP, sans enveloppe Debezium) :
    # une approximation du volume Kafka par changement, pas une mesure de Debezium.
    moteur = create_engine(f"sqlite:///{pipeline.SORTIES / 'oltp.sqlite'}")
    with moteur.connect() as cx:
        lignes = cx.execute(text("SELECT * FROM paiements LIMIT 1000")).mappings().all()
    octets_json = sum(len(json.dumps(dict(ligne), default=str).encode()) for ligne in lignes)
    return {"parquet_faits_octets": parquet.stat().st_size,
            "parquet_octets_par_fait": round(parquet.stat().st_size / faits, 1),
            "entrainement_modele_s": round(entrainement, 1),
            "mesures_modele": mesures_modele, "alertes_flux": alertes,
            "prevalence_fraude_periode_test": round(prevalence_test, 4),
            "json_paiement_octets_moyens": round(octets_json / len(lignes), 1)}


def main(argv: list[str] | None = None) -> None:
    parseur = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sous = parseur.add_subparsers(dest="commande", required=True)
    p = sous.add_parser("postgres", help="mesurer l'occupation disque dans PostgreSQL")
    p.add_argument("--url", required=True, help="URL SQLAlchemy (pilote psycopg)")
    p.add_argument("--jours", type=int, default=60)
    p.add_argument("--par-jour", type=int, default=800)
    sous.add_parser("demo", help="mesurer la démonstration autonome")
    args = parseur.parse_args(argv)
    resultat = (mesurer_postgres(args.url, args.jours, args.par_jour)
                if args.commande == "postgres" else mesurer_demo())
    SORTIES.mkdir(exist_ok=True)
    chemin = SORTIES / f"mesures_{args.commande}.json"
    chemin.write_text(json.dumps(resultat, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(resultat, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
