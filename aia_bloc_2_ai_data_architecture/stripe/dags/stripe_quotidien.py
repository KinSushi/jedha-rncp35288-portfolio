# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""DAG Airflow « stripe_quotidien » : la voie batch du document 05, de l'OLTP à l'entrepôt.

Chaque jour, pour la date logique `ds` : attendre que les paiements du jour soient là, les
extraire en Parquet dans le lac (`paiements/ds=AAAA-MM-JJ/`), les copier dans une table d'étape,
fusionner les faits, rafraîchir les agrégats, contrôler la qualité, publier la partition, puis
réentraîner le modèle si c'est le jour prévu. Huit tâches, dans l'ordre exact du document 05 § 2.

Les tâches appellent les fonctions de la démonstration (SQLite pour l'OLTP, DuckDB pour
l'entrepôt, un dossier pour le lac) : le DAG s'exécute donc tel quel sur un poste, sans AWS.
En production, les mêmes étapes visent Aurora (lecteur), S3 et Redshift ; les tâches y sont
lancées dans le module Terraform `calcul` (définition de tâche `etl`).

CHOIX
-----
- Retenu : la logique de chaque tâche dans une fonction Python ordinaire, appelée par un
  `PythonOperator` ; Airflow n'est importé QUE dans `construire_dag`. Le module s'importe et se
  teste donc sans Airflow installé. Écarté : des opérateurs SQL spécialisés, intestables hors
  d'un vrai Airflow et d'une vraie base.
- Retenu : en local, `fusionner_faits` est le rechargement complet et transactionnel de
  `olap.charger_batch` (réconciliation complète, idempotente) ; en production ce serait un
  `MERGE` sur `id_paiement` depuis la table d'étape. Le contrôle de qualité compare les faits du
  jour à la table d'étape : il prouve que le lac et l'entrepôt disent la même chose.
- Retenu : un contrôle de qualité qui échoue LÈVE une exception : la tâche échoue, la partition
  n'est pas publiée, le modèle n'est pas réentraîné (les tâches suivantes ne partent pas) et
  l'alerte part par `signaler_echec`. Écarté : un résultat « en échec » renvoyé sans exception,
  qu'Airflow compterait comme un succès.
- Retenu : 3 nouvelles tentatives à intervalle croissant, comme le propose le document 05 ;
  intervalle de départ, attente du capteur et jour de réentraînement : hypothèses d'exploitation.
- Écrit pour l'API d'Airflow 2.10 (`airflow.operators.python`, `airflow.sensors.python`).

OÙ
---
- Place : dossier `dags/` du dépôt, à déclarer comme dossier de DAG d'Airflow ; `stripe_demo`
  doit être importable (racine `stripe/` dans le `PYTHONPATH`).
- Dépendances : `stripe_demo.olap` (connexion, chargement, agrégats), `stripe_demo.fraude`
  (entraînement), DuckDB (extension `sqlite` pour lire l'OLTP, Parquet pour le lac).
- Appelants : l'ordonnanceur d'Airflow ; les tests `tests/test_dag_stripe_quotidien.py`.
- Variable d'environnement : `STRIPE_DOSSIER_DONNEES` (dossier de `oltp.sqlite`,
  `olap.duckdb` et `lac/`), par défaut `stripe/sorties`, celui de la démonstration.

COMMENT
-------
- `date_logique` refuse toute date qui n'est pas au format `AAAA-MM-JJ` ; `partition` donne le
  dossier du jour dans le lac ; `sources_pretes` (capteur) dit si l'OLTP contient des paiements
  du jour ; `extraire_oltp_vers_lac` réécrit le fichier Parquet du jour ; `copier_vers_staging`
  recrée `stg_paiements` depuis ce fichier ; `fusionner_faits` recharge l'étoile ;
  `rafraichir_agregats` reconstruit les agrégats ; `controler_qualite` vérifie trois invariants
  (faits du jour = lignes extraites, agrégats = faits, aucune clé orpheline) ;
  `publier_partition` pose le marqueur `_PRET` (écrit en octets : les mêmes sous Windows et
  sous Linux) ; `reentrainer_si_planifie` entraîne le modèle le jour prévu ; `construire_dag`
  assemble les huit tâches. Tous les comptages passent par `compter`, qui rend 0 pour une somme
  sur une table vide (NULL en SQL) au lieu d'échouer.
- Ce qui le prouve (`tests/test_dag_stripe_quotidien.py`, sans Airflow) : analyse du fichier
  (aucun import d'Airflow hors de `construire_dag`, huit tâches dans l'ordre du document 05),
  chaîne complète exécutée sur une mini-démonstration et rejouée sans doublon, refus d'une date
  mal formée, d'une source absente, d'un entrepôt incohérent, texte exact des requêtes envoyées
  à DuckDB (une connexion espion), et graphe des tâches construit avec un faux Airflow minimal
  (dépendances, tentatives, alerte d'échec, dossier par défaut `stripe/sorties`).

POURQUOI
--------
- Les huit tâches, leur idempotence et la politique de reprise :
  `livrables/aia2/05_pipeline.md:34-69`.
- Les invariants de qualité : ceux de `tests/test_olap.py`, repris par
  `livrables/aia2/10_continuite_supervision_maintenance.md:120-122`.
- L'API d'Airflow 2.10 : voir la documentation d'Apache Airflow ; le DAG a été
  importé par Airflow 2.10.5 hors de ce dépôt (voir le document 05 § 2), pas exécuté par lui.

JOURNALISATION
--------------
- Journal `stripe_quotidien` (module `logging`) : une ligne ERROR « ALERTE » par tâche en échec,
  écrite par `signaler_echec`, que l'ordonnanceur appelle en dernier recours ; en production, la
  même alerte part vers la rubrique SNS du module Terraform `supervision`. Les tâches elles-mêmes
  ne journalisent rien : leurs résultats sont les valeurs de retour, gardées par Airflow (XCom).
  Toute erreur se propage à Airflow, qui la note et retente ; rien n'est avalé.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from stripe_demo import fraude, olap

ID_DAG = "stripe_quotidien"
TACHES = ("attendre_sources", "extraire_oltp_vers_lac", "copier_vers_staging", "fusionner_faits",
          "rafraichir_agregats", "controles_qualite", "publier_partition",
          "reentrainer_si_planifie")
TENTATIVES = 3
DELAI_TENTATIVE = timedelta(minutes=5)  # hypothèse : doublé à chaque nouvelle tentative
ATTENTE_CAPTEUR = timedelta(minutes=10)  # hypothèse : intervalle de vérification des sources
LIMITE_CAPTEUR = timedelta(hours=6)  # hypothèse : au-delà, la tâche échoue et l'alerte part
JOUR_REENTRAINEMENT = 1  # hypothèse : le lundi (isoweekday), voir le document 07
DOSSIER_PAR_DEFAUT = Path(__file__).resolve().parent.parent / "sorties"

JOURNAL = logging.getLogger(ID_DAG)


class ControleQualiteEchoue(RuntimeError):
    """Un invariant de qualité est violé : la partition du jour ne doit pas être publiée."""


def date_logique(ds: str) -> date:
    """La date logique d'Airflow (`ds`), strictement au format AAAA-MM-JJ."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ds):
        raise ValueError(f"date logique attendue au format AAAA-MM-JJ : {ds!r}")
    return date.fromisoformat(ds)


def chemins(dossier: Path) -> dict[str, str]:
    """Emplacements de l'OLTP, de l'entrepôt et du lac dans le dossier de données."""
    return {"chemin_oltp": str(dossier / "oltp.sqlite"),
            "chemin_olap": str(dossier / "olap.duckdb"),
            "dossier_lac": str(dossier / "lac")}


def partition(dossier_lac: str, ds: str) -> Path:
    """Dossier du jour dans le lac : paiements/ds=AAAA-MM-JJ/."""
    return Path(dossier_lac) / "paiements" / f"ds={date_logique(ds).isoformat()}"


def _oltp_en_lecture(chemin_oltp: str) -> duckdb.DuckDBPyConnection:
    """Connexion DuckDB en mémoire, l'OLTP attaché en lecture seule sous le nom `oltp`."""
    if not Path(chemin_oltp).is_file():
        raise FileNotFoundError(f"base OLTP introuvable : {chemin_oltp}")
    cx = duckdb.connect()
    cx.execute("INSTALL sqlite; LOAD sqlite;")
    cx.execute(f"ATTACH '{Path(chemin_oltp).as_posix()}' AS oltp (TYPE sqlite, READ_ONLY)")
    return cx


def compter(cx: duckdb.DuckDBPyConnection, requete: str) -> int:
    """Première colonne de la première ligne, en entier ; 0 si NULL (somme sur du vide)."""
    ligne = cx.execute(requete).fetchone()
    return int(ligne[0]) if ligne and ligne[0] is not None else 0


def sources_pretes(ds: str, chemin_oltp: str) -> bool:
    """Capteur : vrai dès que l'OLTP contient au moins un paiement du jour."""
    jour = date_logique(ds)
    if not Path(chemin_oltp).is_file():
        return False
    with _oltp_en_lecture(chemin_oltp) as cx:
        ligne = cx.execute("SELECT count(*) FROM oltp.paiements "
                           "WHERE CAST(cree_le AS DATE) = ?", [jour]).fetchone()
    return ligne is not None and ligne[0] > 0


def extraire_oltp_vers_lac(ds: str, chemin_oltp: str, dossier_lac: str) -> int:
    """Écrit les paiements du jour en Parquet ; le fichier est réécrit à chaque relance."""
    jour = date_logique(ds)
    cible = partition(dossier_lac, ds) / "paiements.parquet"
    with _oltp_en_lecture(chemin_oltp) as cx:
        cible.parent.mkdir(parents=True, exist_ok=True)
        cx.execute(f"""COPY (SELECT * FROM oltp.paiements
                            WHERE CAST(cree_le AS DATE) = DATE '{jour.isoformat()}'
                            ORDER BY id_paiement)
                       TO '{cible.as_posix()}' (FORMAT PARQUET)""")
        return compter(cx, f"SELECT count(*) FROM read_parquet('{cible.as_posix()}')")


def copier_vers_staging(ds: str, dossier_lac: str, chemin_olap: str) -> int:
    """Recrée la table d'étape `stg_paiements` à partir du fichier Parquet du jour."""
    fichier = partition(dossier_lac, ds) / "paiements.parquet"
    if not fichier.is_file():
        raise FileNotFoundError(f"partition du jour absente du lac : {fichier}")
    with olap.connecter(chemin_olap) as cx:
        cx.execute("CREATE OR REPLACE TABLE stg_paiements AS "
                   f"SELECT * FROM read_parquet('{fichier.as_posix()}')")
        return compter(cx, "SELECT count(*) FROM stg_paiements")


def fusionner_faits(chemin_oltp: str, chemin_olap: str) -> int:
    """Recharge dimensions et faits depuis l'OLTP, en une transaction (`olap.charger_batch`)."""
    with olap.connecter(chemin_olap) as cx:
        return olap.charger_batch(cx, chemin_oltp)["faits"]


def rafraichir_agregats(chemin_olap: str) -> None:
    """Reconstruit les tables d'agrégats (les vues matérialisées de Redshift)."""
    with olap.connecter(chemin_olap) as cx:
        olap.construire_agregats(cx)


def controler_qualite(ds: str, chemin_olap: str) -> dict[str, int]:
    """Trois invariants ; le premier écart arrête le DAG (`ControleQualiteEchoue`)."""
    cle_date = int(date_logique(ds).strftime("%Y%m%d"))
    with olap.connecter(chemin_olap) as cx:
        extraits = compter(cx, "SELECT count(*) FROM stg_paiements")
        faits_du_jour = compter(
            cx, f"SELECT count(*) FROM faits_paiements WHERE cle_date = {cle_date}")
        faits = compter(cx, "SELECT count(*) FROM faits_paiements")
        agreges = compter(cx, "SELECT sum(nb_paiements) FROM agr_chiffre_quotidien")
        orphelins = compter(cx, """
            SELECT count(*) FROM faits_paiements f
            WHERE f.cle_date NOT IN (SELECT cle_date FROM dim_date)
               OR f.cle_marchand NOT IN (SELECT cle_marchand FROM dim_marchand)
               OR f.cle_client NOT IN (SELECT cle_client FROM dim_client)""")
    ecarts = []
    if faits_du_jour != extraits:
        ecarts.append(f"faits du jour {faits_du_jour} au lieu de {extraits} paiements extraits")
    if agreges != faits:
        ecarts.append(f"agrégats {agreges} au lieu de {faits} faits")
    if orphelins:
        ecarts.append(f"{orphelins} fait(s) avec une clé de dimension orpheline")
    if ecarts:
        raise ControleQualiteEchoue(" ; ".join(ecarts))
    return {"paiements_du_jour": extraits, "faits": faits}


def publier_partition(ds: str, dossier_lac: str) -> str:
    """Pose le marqueur `_PRET` : les lecteurs du lac ne prennent que les partitions publiées."""
    dossier = partition(dossier_lac, ds)
    if not (dossier / "paiements.parquet").is_file():
        raise FileNotFoundError(f"rien à publier : {dossier}")
    marqueur = dossier / "_PRET"
    marqueur.write_bytes(date_logique(ds).isoformat().encode() + b"\n")
    return str(marqueur)


def reentrainer_si_planifie(ds: str, chemin_olap: str,
                            jour_semaine: int = JOUR_REENTRAINEMENT) -> dict[str, Any] | None:
    """Le jour prévu, entraîne le modèle sur l'entrepôt et rend ses mesures ; sinon `None`."""
    if date_logique(ds).isoweekday() != jour_semaine:
        return None
    with olap.connecter(chemin_olap) as cx:
        _modele, mesures, _scores = fraude.entrainer(cx)
    return {"version": fraude.VERSION_MODELE, **mesures}


def signaler_echec(contexte: Mapping[str, Any]) -> None:
    """Rappel d'échec d'Airflow : une ligne ERROR « ALERTE » (en production : la rubrique SNS)."""
    instance = contexte.get("task_instance")
    tache = getattr(instance, "task_id", "inconnue")
    JOURNAL.error("ALERTE %s : la tâche %s a échoué pour la date %s",
                  ID_DAG, tache, contexte.get("ds", "inconnue"))


def construire_dag(dossier: Path) -> Any:
    """Assemble les huit tâches. Seule fonction qui importe Airflow."""
    from airflow import DAG
    from airflow.operators.python import PythonOperator
    from airflow.sensors.python import PythonSensor

    c = chemins(dossier)
    arguments = {"owner": "donnees", "retries": TENTATIVES, "retry_delay": DELAI_TENTATIVE,
                 "retry_exponential_backoff": True, "on_failure_callback": signaler_echec}
    with DAG(dag_id=ID_DAG, schedule="@daily", start_date=datetime(2026, 8, 1, tzinfo=UTC),
             catchup=False, max_active_runs=1, default_args=arguments,
             tags=["stripe", "batch"]) as dag:
        capteur = PythonSensor(task_id="attendre_sources", python_callable=sources_pretes,
                               op_kwargs={"chemin_oltp": c["chemin_oltp"]}, mode="reschedule",
                               poke_interval=ATTENTE_CAPTEUR, timeout=LIMITE_CAPTEUR)
        etapes = [
            ("extraire_oltp_vers_lac", extraire_oltp_vers_lac, ("chemin_oltp", "dossier_lac")),
            ("copier_vers_staging", copier_vers_staging, ("dossier_lac", "chemin_olap")),
            ("fusionner_faits", fusionner_faits, ("chemin_oltp", "chemin_olap")),
            ("rafraichir_agregats", rafraichir_agregats, ("chemin_olap",)),
            ("controles_qualite", controler_qualite, ("chemin_olap",)),
            ("publier_partition", publier_partition, ("dossier_lac",)),
            ("reentrainer_si_planifie", reentrainer_si_planifie, ("chemin_olap",)),
        ]
        precedente = capteur
        for identifiant, fonction, parametres in etapes:
            tache = PythonOperator(task_id=identifiant, python_callable=fonction,
                                   op_kwargs={nom: c[nom] for nom in parametres})
            precedente >> tache
            precedente = tache
    return dag


try:  # Airflow présent : le DAG est publié pour l'ordonnanceur ; absent : le module reste utile.
    import airflow  # noqa: F401
except ModuleNotFoundError:
    dag = None
else:
    dag = construire_dag(Path(os.environ.get("STRIPE_DOSSIER_DONNEES", DOSSIER_PAR_DEFAUT)))
