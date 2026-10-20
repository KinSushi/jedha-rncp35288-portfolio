# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""DAG de supervision : contrôle de santé du flux temps réel toutes les 10 minutes.

Le flux temps réel tourne hors d'Airflow (producteur et consommateur sont des services continus).
Ce DAG en est le garde-fou : il vérifie la fraîcheur des données, les notifications en attente ou
en échec, le taux de rejets et la présence d'un modèle en service. Une anomalie envoie une
notification puis fait échouer la tâche (sans relance), ce qui la rend visible dans l'interface.

CHOIX
-----
- Retenu : une seule tâche, ``controler_sante``, qui échoue sans relance quand il y a un constat :
  l'incident est visible dans l'interface d'Airflow, pas seulement dans une boîte aux lettres.
- Retenu : une exécution toutes les 10 minutes, 1 relance après 1 minute, délai maximal de
  5 minutes et SLA de 5 minutes.
- Retenu : ``CronDataIntervalTimetable`` explicite, ``catchup=False`` et ``max_active_runs=1``.
- Écarté : aucune alternative n'est documentée (source : inconnue) ; le flux tourne hors
  d'Airflow et ce DAG en est le garde-fou.

OÙ
---
- Place : dossier ``dags``, lu par le planificateur Airflow de l'image Docker.
- Appelants : Airflow ; le DAG n'appelle que ``detection_fraude.taches_airflow.controler_sante``.
- Dépendances : ``airflow``, ``pendulum`` et le paquet ``detection_fraude``.

COMMENT
-------
1. Une tâche ``PythonOperator`` appelle ``controler_sante`` : contrôles de ``supervision``, puis,
   s'il y a des constats, notification et échec définitif.
Tests qui le prouvent : test_supervision_toutes_les_dix_minutes (tests/test_dags.py:175-178),
test_chaque_dag_configure_relances_delais_sla_et_alertes (tests/test_dags.py:115-124) et
test_supervision_en_echec_notifie_et_fait_echouer_la_tache (tests/test_bout_en_bout.py:255-259).

POURQUOI
--------
- Les SLA et leur contrôle, comment l'alerte arrive : livrables/aia3/04_exploitation.md:41-90.
- Le DAG de supervision dans la liste des DAG : livrables/aia3/02_pipeline_et_qualite.md:145-164.
- Première exécution échouée comme prévu, puis au vert : livrables/aia3/04_exploitation.md:78-90.
- Paramètres du DAG : dags/fraude_supervision.py:69.
- Indicateur 6.1 du référentiel AIA RNCP41993 : texte du référentiel à joindre au dossier.

JOURNALISATION
--------------
Le fichier du DAG n'écrit aucun journal. Les constats graves sont journalisés par le module de
supervision (événement ``constat_supervision``, WARNING) et notifiés ; la tâche en échec apparaît
dans l'interface d'Airflow.
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
    "retries": 1,
    "retry_delay": timedelta(minutes=1),
    "execution_timeout": timedelta(minutes=5),
    "sla": timedelta(minutes=5),
    "on_failure_callback": taches.alerte_echec_tache,
}

with DAG(
    dag_id="fraude_supervision",
    description="Contrôles de santé du flux temps réel (fraîcheur, alertes, rejets, modèle).",
    schedule=CronDataIntervalTimetable("*/10 * * * *", timezone=FUSEAU),
    start_date=pendulum.datetime(2026, 10, 1, tz=FUSEAU),
    catchup=False,
    max_active_runs=1,
    default_args=ARGS_PAR_DEFAUT,
    sla_miss_callback=taches.alerte_sla,
    tags=["fraude", "supervision"],
    doc_md=__doc__,
) as dag:
    PythonOperator(task_id="controler_sante", python_callable=taches.controler_sante)
