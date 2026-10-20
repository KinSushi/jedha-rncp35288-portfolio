# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""DAG Airflow : ordonnancement, dépendances, relances, SLA — critères 3.1, 3.2, 4.3 et 6.1.

Airflow ne s'installe pas sur tous les postes (Windows notamment). Ces tests substituent donc à
Airflow une doublure minimale qui *enregistre* ce que le fichier de DAG construit : tâches,
dépendances (``>>``), paramètres du DAG et arguments par défaut. Le DAG réel est en outre analysé
par le vrai Airflow dans l'image officielle (voir
``livrables/aia3/07_couverture_et_limites.md``), et le dernier test de ce fichier s'exécute si
Airflow est installé.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from detection_fraude import taches_airflow
from detection_fraude.configuration import RACINE_PROJET

DOSSIER_DAGS = RACINE_PROJET / "dags"
FICHIERS = sorted(DOSSIER_DAGS.glob("fraude_*.py"))


class FauxOperateur:
    def __init__(self, task_id, python_callable=None, **kwargs):
        self.task_id, self.python_callable, self.kwargs = task_id, python_callable, kwargs
        self.upstream: set[FauxOperateur] = set()
        self.downstream: set[FauxOperateur] = set()
        FauxDAG.courant.taches.append(self)

    def __rshift__(self, autre):
        self.downstream.add(autre)
        autre.upstream.add(self)
        return autre


class FauxHoraire:
    """Doublure de ``CronDataIntervalTimetable`` : garde l'expression cron et le fuseau."""

    def __init__(self, cron, timezone=None):
        self.cron, self.timezone = cron, timezone


class FauxDAG:
    courant: FauxDAG

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.dag_id = kwargs["dag_id"]
        self.taches: list[FauxOperateur] = []

    def __enter__(self):
        FauxDAG.courant = self
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def charger_dag(monkeypatch):
    """Importe un fichier de DAG avec Airflow et pendulum remplacés par des doublures."""
    airflow = types.ModuleType("airflow")
    airflow.DAG = FauxDAG
    operateurs = types.ModuleType("airflow.operators")
    python = types.ModuleType("airflow.operators.python")
    python.PythonOperator = FauxOperateur
    timetables = types.ModuleType("airflow.timetables")
    interval = types.ModuleType("airflow.timetables.interval")
    interval.CronDataIntervalTimetable = FauxHoraire
    pendulum = types.ModuleType("pendulum")
    pendulum.timezone = lambda nom: nom
    pendulum.datetime = lambda *a, tz=None: datetime(*a)
    for nom, module in {
        "airflow": airflow,
        "airflow.operators": operateurs,
        "airflow.operators.python": python,
        "airflow.timetables": timetables,
        "airflow.timetables.interval": interval,
        "pendulum": pendulum,
    }.items():
        monkeypatch.setitem(sys.modules, nom, module)

    def charger(chemin: Path) -> tuple[FauxDAG, dict]:
        spec = importlib.util.spec_from_file_location(f"dag_{chemin.stem}", chemin)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.dag, vars(module)

    return charger


def test_les_trois_dags_existent():
    assert [f.stem for f in FICHIERS] == [
        "fraude_rapport_du_matin",
        "fraude_reentrainement_hebdo",
        "fraude_supervision",
    ]


@pytest.mark.parametrize("fichier", FICHIERS, ids=lambda f: f.stem)
def test_chaque_dag_est_planifie_sans_lancement_manuel(charger_dag, fichier):
    dag, _ = charger_dag(fichier)
    assert dag.kwargs["schedule"].cron, "un DAG sans planification imposerait un lancement manuel"
    assert dag.kwargs["schedule"].timezone == "Europe/Paris"
    assert dag.kwargs["catchup"] is False
    assert dag.kwargs["max_active_runs"] == 1  # jamais deux exécutions concurrentes


@pytest.mark.parametrize("fichier", FICHIERS, ids=lambda f: f.stem)
def test_chaque_dag_configure_relances_delais_sla_et_alertes(charger_dag, fichier):
    dag, _ = charger_dag(fichier)
    args = dag.kwargs["default_args"]
    assert args["retries"] >= 1
    assert isinstance(args["retry_delay"], timedelta) and args["retry_delay"] > timedelta(0)
    assert isinstance(args["sla"], timedelta) and args["sla"] > timedelta(0)
    assert isinstance(args["execution_timeout"], timedelta)
    assert callable(args["on_failure_callback"])  # alerte quand les relances sont épuisées
    assert callable(dag.kwargs["sla_miss_callback"])  # alerte quand le SLA est dépassé


@pytest.mark.parametrize("fichier", FICHIERS, ids=lambda f: f.stem)
def test_chaque_tache_appelle_une_fonction_du_paquet(charger_dag, fichier):
    dag, _ = charger_dag(fichier)
    for tache in dag.taches:
        assert tache.python_callable is getattr(taches_airflow, tache.python_callable.__name__)


def chaine(dag):
    """Ordre des tâches si le graphe est une chaîne ; sinon échec explicite."""
    racines = [t for t in dag.taches if not t.upstream]
    assert len(racines) == 1, "une seule tâche de départ"
    ordre, courante = [], racines[0]
    while courante:
        ordre.append(courante.task_id)
        assert len(courante.downstream) <= 1
        courante = next(iter(courante.downstream), None)
    assert len(ordre) == len(dag.taches), "aucune tâche orpheline"
    return ordre


def test_rapport_du_matin_a_7h_chaque_etape_attend_la_precedente(charger_dag):
    dag, _ = charger_dag(DOSSIER_DAGS / "fraude_rapport_du_matin.py")
    assert dag.kwargs["schedule"].cron == "0 7 * * *"
    assert chaine(dag) == [
        "verifier_donnees_de_la_veille",
        "calculer_et_ecrire_rapport",
        "notifier_rapport_du_matin",
        "controler_sla_rapport",
    ]
    args = dag.kwargs["default_args"]
    assert args["retries"] == 3 and args["retry_delay"] == timedelta(minutes=5)
    assert args["sla"] == timedelta(minutes=60)  # livré avant 08:00


def test_reentrainement_hebdomadaire_avec_barriere_avant_promotion(charger_dag):
    dag, _ = charger_dag(DOSSIER_DAGS / "fraude_reentrainement_hebdo.py")
    assert dag.kwargs["schedule"].cron == "0 3 * * 0"  # dimanche
    ordre = chaine(dag)
    assert ordre == [
        "verifier_jeu_de_donnees",
        "entrainer_candidats",
        "valider_barriere_qualite",
        "promouvoir_modele",
        "notifier_reentrainement",
    ]
    assert ordre.index("valider_barriere_qualite") < ordre.index("promouvoir_modele")


def test_supervision_toutes_les_dix_minutes(charger_dag):
    dag, _ = charger_dag(DOSSIER_DAGS / "fraude_supervision.py")
    assert dag.kwargs["schedule"].cron == "*/10 * * * *"
    assert [t.task_id for t in dag.taches] == ["controler_sante"]


def test_aucun_secret_dans_les_dags():
    for fichier in FICHIERS:
        texte = fichier.read_text(encoding="utf-8").lower()
        for mot in ("password", "mot_de_passe", "token", "smtp_", "api_key"):
            assert mot not in texte


def test_les_dags_importent_tous_depuis_le_paquet_et_pas_de_logique_metier():
    for fichier in FICHIERS:
        texte = fichier.read_text(encoding="utf-8")
        assert "from detection_fraude import taches_airflow" in texte
        assert "sqlalchemy" not in texte and "pandas" not in texte


def test_vrai_airflow_analyse_les_dags_sans_erreur():
    pytest.importorskip("airflow")
    from airflow.models import DagBag

    bag = DagBag(dag_folder=str(DOSSIER_DAGS), include_examples=False)
    assert bag.import_errors == {}
    assert set(bag.dag_ids) >= {
        "fraude_rapport_du_matin",
        "fraude_reentrainement_hebdo",
        "fraude_supervision",
    }
