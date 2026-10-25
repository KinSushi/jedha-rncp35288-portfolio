# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""DAG du rapport du matin : paiements et fraudes de la veille, chaque jour à 07:00.

Pourquoi un traitement par lot : le métier lit ce rapport le matin ; une journée complète est
consolidée une seule fois, sans exigence de fraîcheur à la seconde (l'alerte immédiate, elle, est
traitée en flux par le consommateur).

Enchaînement (chaque tâche attend le succès de la précédente) :

    verifier_donnees_de_la_veille >> calculer_et_ecrire_rapport
        >> notifier_rapport_du_matin >> controler_sla_rapport

CHOIX
-----
- Retenu : ``CronDataIntervalTimetable`` explicite, pour que le SLA d'Airflow (calculé à partir de
  la fin de l'intervalle de données) soit mesuré à partir de 07:00 le jour même, et non le
  lendemain (comportement de la planification « déclencheur » par défaut des versions récentes).
- Retenu : 3 relances espacées de 5 minutes (attente croissante, plafonnée à 30 minutes), délai
  maximal de 20 minutes par tâche, SLA de 60 minutes après l'heure prévue.
- Retenu : ``catchup=False`` et ``max_active_runs=1`` : jamais deux exécutions concurrentes ; un
  jour manqué se rattrape avec ``airflow dags backfill``, jamais à la main sur le fichier.
- Retenu : alerte à l'épuisement des relances (``on_failure_callback``) et au dépassement du SLA
  (``sla_miss_callback``).
- Écarté : un lancement manuel (le DAG est planifié et actif dès sa création) ; aucune logique
  métier dans ce fichier.

OÙ
---
- Place : dossier ``dags``, lu par le planificateur Airflow de l'image Docker.
- Appelants : Airflow ; le DAG n'appelle que des fonctions de ``detection_fraude.taches_airflow``.
- Dépendances : ``airflow`` (DAG, PythonOperator, CronDataIntervalTimetable), ``pendulum`` et le
  paquet ``detection_fraude``.

COMMENT
-------
1. Le DAG déclare quatre tâches ``PythonOperator`` et les enchaîne avec ``>>``.
2. Chaque tâche appelle la fonction du même nom du module ``taches_airflow`` ; les arguments par
   défaut portent les relances, le délai maximal, le SLA et l'alerte d'échec.
Tests qui le prouvent : test_rapport_du_matin_a_7h_chaque_etape_attend_la_precedente
(tests/test_dags.py:147-158), test_chaque_dag_est_planifie_sans_lancement_manuel
(tests/test_dags.py:106-112), test_chaque_dag_configure_relances_delais_sla_et_alertes
(tests/test_dags.py:115-124) et test_vrai_airflow_analyse_les_dags_sans_erreur
(tests/test_dags.py:195-205) (exécuté seulement si Airflow est installé).

POURQUOI
--------
- Les trois DAG, leurs planifications, relances et SLA :
  livrables/aia3/02_pipeline_et_qualite.md:145-164.
- Dépendances entre tâches du rapport du matin : livrables/aia3/02_pipeline_et_qualite.md:166-187.
- Pourquoi un lot pour ce rapport : livrables/aia3/01_architecture.md:121-127.
- Paramètres du DAG : dags/fraude_rapport_du_matin.py:84.
- Indicateurs 3.1 et 3.2 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Le fichier du DAG n'écrit aucun journal : il déclare l'ordonnancement. Les journaux sont ceux de
l'interface d'Airflow (historique des exécutions, relances, SLA) et les événements des fonctions
appelées (``donnees_de_la_veille_ok``, ``rapport_produit``, ``sla_depasse``).
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
    "retries": 3,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),
    "execution_timeout": timedelta(minutes=20),
    "sla": timedelta(minutes=60),
    "on_failure_callback": taches.alerte_echec_tache,
}

with DAG(
    dag_id="fraude_rapport_du_matin",
    description="Rapport quotidien des paiements et des fraudes de la veille (07:00).",
    schedule=CronDataIntervalTimetable("0 7 * * *", timezone=FUSEAU),
    start_date=pendulum.datetime(2026, 10, 1, tz=FUSEAU),
    catchup=False,
    max_active_runs=1,
    default_args=ARGS_PAR_DEFAUT,
    sla_miss_callback=taches.alerte_sla,
    tags=["fraude", "lot", "rapport"],
    doc_md=__doc__,
) as dag:
    verifier = PythonOperator(
        task_id="verifier_donnees_de_la_veille",
        python_callable=taches.verifier_donnees_de_la_veille,
    )
    calculer = PythonOperator(
        task_id="calculer_et_ecrire_rapport",
        python_callable=taches.calculer_et_ecrire_rapport,
    )
    notifier = PythonOperator(
        task_id="notifier_rapport_du_matin",
        python_callable=taches.notifier_rapport_du_matin,
    )
    controler_sla = PythonOperator(
        task_id="controler_sla_rapport",
        python_callable=taches.controler_sla_rapport,
    )

    verifier >> calculer >> notifier >> controler_sla
