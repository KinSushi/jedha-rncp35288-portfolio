# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""DAG de réentraînement hebdomadaire du modèle (dimanche, 03:00).

Enchaînement (chaque tâche attend le succès de la précédente) :

    verifier_jeu_de_donnees >> entrainer_candidats >> valider_barriere_qualite
        >> promouvoir_modele >> notifier_reentrainement

Un modèle entraîné n'est jamais mis en service automatiquement : il doit franchir une barrière de
qualité (plancher de PR-AUC et absence de régression par rapport au modèle en service). Un refus
est un échec définitif, non relancé, qui déclenche l'alerte ; l'ancien modèle reste en service.
La promotion est une bascule atomique de pointeur que le consommateur détecte sans redémarrer.
Chaque exécution est tracée dans MLflow (paramètres, métriques, empreinte des données).

CHOIX
-----
- Retenu : la barrière de qualité entre l'entraînement et la promotion :
  ``valider_barriere_qualite`` précède ``promouvoir_modele`` dans la chaîne (vérifié par un test).
- Retenu : un refus de la barrière est un échec définitif, non relancé, qui déclenche l'alerte.
- Retenu : 2 relances espacées de 10 minutes (plafond d'une heure), délai maximal de 2 heures par
  tâche, SLA de 3 heures.
- Retenu : ``CronDataIntervalTimetable`` explicite, ``catchup=False`` et ``max_active_runs=1``.
- Écarté : la mise en service automatique d'un modèle entraîné sans barrière (le module
  d'entraînement l'interdit explicitement).

OÙ
---
- Place : dossier ``dags``, lu par le planificateur Airflow de l'image Docker.
- Appelants : Airflow ; le DAG n'appelle que des fonctions de ``detection_fraude.taches_airflow``.
- Dépendances : ``airflow``, ``pendulum`` et le paquet ``detection_fraude``.
- Aval : le consommateur recharge le modèle promu sans redémarrer (``ChargeurModele``).

COMMENT
-------
1. Cinq tâches ``PythonOperator`` enchaînées par ``>>``.
2. Les arguments par défaut portent les relances, le délai maximal, le SLA et l'alerte d'échec.
Tests qui le prouvent : test_reentrainement_hebdomadaire_avec_barriere_avant_promotion
(tests/test_dags.py:161-172), test_chaque_dag_est_planifie_sans_lancement_manuel
(tests/test_dags.py:106-112), test_chaque_tache_appelle_une_fonction_du_paquet
(tests/test_dags.py:127-131) et test_les_taches_du_dag_de_reentrainement_et_la_barriere
(tests/test_bout_en_bout.py:230-241).

POURQUOI
--------
- Planification, relances et SLA du réentraînement :
  livrables/aia3/02_pipeline_et_qualite.md:145-164.
- Barrière de qualité, échec définitif, promotion atomique :
  livrables/aia3/02_pipeline_et_qualite.md:166-187.
- Réentraînement : lot sans contrainte de latence : livrables/aia3/01_architecture.md:129-132.
- Paramètres du DAG : dags/fraude_reentrainement_hebdo.py:83.
- Indicateurs 3.1 et 3.2 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Le fichier du DAG n'écrit aucun journal. Les journaux sont ceux de l'interface d'Airflow et les
événements des fonctions appelées : ``jeu_prepare``, ``candidat_evalue``, ``barriere_qualite``,
``modele_promu``, ``entrainement_termine`` (voir le module d'entraînement).
"""

from datetime import timedelta

import pendulum
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.timetables.interval import CronDataIntervalTimetable

from detection_fraude import taches_airflow as taches

FUSEAU = pendulum.timezone("Europe/Paris")

ARGS_PAR_DEFAUT = {
    "owner": "equipe-donnees",
    "retries": 2,
    "retry_delay": timedelta(minutes=10),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(hours=1),
    "execution_timeout": timedelta(hours=2),
    "sla": timedelta(hours=3),
    "on_failure_callback": taches.alerte_echec_tache,
}

with DAG(
    dag_id="fraude_reentrainement_hebdo",
    description="Réentraînement hebdomadaire, barrière de qualité puis promotion du modèle.",
    schedule=CronDataIntervalTimetable("0 3 * * 0", timezone=FUSEAU),
    start_date=pendulum.datetime(2026, 10, 4, tz=FUSEAU),
    catchup=False,
    max_active_runs=1,
    default_args=ARGS_PAR_DEFAUT,
    sla_miss_callback=taches.alerte_sla,
    tags=["fraude", "lot", "modele"],
    doc_md=__doc__,
) as dag:
    verifier = PythonOperator(
        task_id="verifier_jeu_de_donnees",
        python_callable=taches.verifier_jeu_de_donnees,
    )
    entrainer = PythonOperator(
        task_id="entrainer_candidats",
        python_callable=taches.entrainer_candidats,
    )
    valider = PythonOperator(
        task_id="valider_barriere_qualite",
        python_callable=taches.valider_barriere_qualite,
    )
    promouvoir = PythonOperator(
        task_id="promouvoir_modele",
        python_callable=taches.promouvoir_modele,
    )
    notifier = PythonOperator(
        task_id="notifier_reentrainement",
        python_callable=taches.notifier_reentrainement,
    )

    verifier >> entrainer >> valider >> promouvoir >> notifier
