# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Fixtures communes : configuration isolée, entrepôt temporaire, jeu synthétique, modèle."""

from __future__ import annotations

import logging
import os
import sys
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

import detection_fraude.file_attente as file_attente_module  # noqa: E402
from detection_fraude.configuration import Configuration, charger_configuration  # noqa: E402
from detection_fraude.entrainement import entrainer  # noqa: E402
from detection_fraude.entrepot import Entrepot  # noqa: E402
from detection_fraude.masquage import HacheurCarte  # noqa: E402
from tests.helpers import fabriquer_jeu  # noqa: E402

SEL_DE_TEST = (
    "sel-de-test-uniquement-0123456789abcdef"  # valeur de test, sans lien avec la production
)


class Evenements(logging.Handler):
    """Collecte les événements du journal ``fraude`` (qui ne se propage pas à la racine).

    Chaque événement est un triplet ``(niveau, nom de l'événement, champs)`` : les tests peuvent
    ainsi vérifier ce que l'exploitation verra réellement dans l'audit, champ par champ.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.enregistrements: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.enregistrements.append(record)

    def tous(self) -> list[tuple[str, str, dict[str, Any]]]:
        return [
            (r.levelname, r.getMessage(), dict(getattr(r, "champs", {})))
            for r in self.enregistrements
        ]

    def nommes(self, nom: str) -> list[tuple[str, dict[str, Any]]]:
        """``(niveau, champs)`` de chaque événement portant ce nom."""
        return [(niveau, champs) for niveau, evenement, champs in self.tous() if evenement == nom]


@pytest.fixture
def evenements() -> Iterator[Evenements]:
    """Branche un collecteur sur le journal ``fraude`` pendant la durée du test."""
    journal = logging.getLogger("fraude")
    collecteur = Evenements()
    niveau_initial = journal.level
    journal.addHandler(collecteur)
    journal.setLevel(logging.DEBUG)
    try:
        yield collecteur
    finally:
        journal.removeHandler(collecteur)
        journal.setLevel(niveau_initial)


@pytest.fixture(autouse=True)
def isoler_mlflow():
    """MLflow écrit lui-même ``MLFLOW_TRACKING_URI`` dans l'environnement : on le nettoie."""
    os.environ.pop("MLFLOW_TRACKING_URI", None)
    yield
    os.environ.pop("MLFLOW_TRACKING_URI", None)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Configuration:
    """Configuration « dev » dont tous les chemins pointent dans un dossier temporaire."""
    monkeypatch.setenv("FRAUDE_DONNEES", str(tmp_path / "donnees"))
    monkeypatch.setenv("FRAUDE_SEL_HACHAGE", SEL_DE_TEST)
    monkeypatch.setenv("FRAUDE_JEU_CHEMIN", str(tmp_path / "jeu.csv"))
    for variable in (
        "FRAUDE_DSN_ENTREPOT",
        "FRAUDE_WEBHOOK_URL",
        "FRAUDE_ENV",
        "FRAUDE_TYPE_FILE",
    ):
        monkeypatch.delenv(variable, raising=False)
    return charger_configuration("dev")


@pytest.fixture
def hacheur(config: Configuration) -> HacheurCarte:
    return HacheurCarte(config.sel_hachage(), config.longueur_hash)


@pytest.fixture
def entrepot(config: Configuration):
    base = Entrepot(config.dsn_entrepot())
    base.initialiser()
    yield base
    base.fermer()


@pytest.fixture
def instructions_sql(entrepot):
    """Les instructions envoyées au pilote SQLite, dans l'ordre, telles qu'il les reçoit."""
    envoyees: list[str] = []

    def noter(connexion, curseur, instruction, parametres, contexte, plusieurs):
        envoyees.append(instruction)

    event.listen(entrepot.moteur, "before_cursor_execute", noter)
    yield envoyees
    event.remove(entrepot.moteur, "before_cursor_execute", noter)


@pytest.fixture(scope="session")
def jeu_synthetique() -> object:
    return fabriquer_jeu()


@pytest.fixture(scope="session")
def modele_entraine(tmp_path_factory: pytest.TempPathFactory, jeu_synthetique) -> dict:
    """Un entraînement complet sur le jeu synthétique, partagé par les tests qui en ont besoin."""
    racine = tmp_path_factory.mktemp("entrainement")
    csv = racine / "jeu.csv"
    jeu_synthetique.to_csv(csv, index=False)
    mp = pytest.MonkeyPatch()
    mp.setenv("FRAUDE_DONNEES", str(racine / "donnees"))
    mp.setenv("FRAUDE_SEL_HACHAGE", SEL_DE_TEST)
    mp.setenv("FRAUDE_JEU_CHEMIN", str(csv))
    try:
        configuration = charger_configuration("dev")
        resultat = entrainer(configuration, csv, suivre_mlflow=False, promouvoir_si_premier=True)
    finally:
        mp.undo()
    return {"config": configuration, "resultat": resultat, "csv": csv}


# Les tests qui exigent un parcours complet (démonstration locale, entraînement d'un
# modèle) ou le jeu synthétique complet (6 000 lignes) passent en dernier. Ils sont lents et
# un défaut du code peut les faire attendre longtemps (une file jamais vidée, des tranches
# relues en boucle) : les tests rapides, qui voient le même défaut en quelques millisecondes,
# doivent parler les premiers. L'ordre des autres tests est conservé (le tri est stable).
FIXTURES_LOURDES = frozenset({"parcours", "modele_entraine", "jeu_synthetique"})


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    items.sort(key=lambda item: bool(FIXTURES_LOURDES & set(item.fixturenames)))


# Garde-fous communs : un défaut du code doit faire ÉCHOUER un test, jamais le bloquer.
PAUSES_MAX_PAR_TEST = 10_000


class TempsBorne:
    """Le module ``time`` tel que le voit ``file_attente``, avec un nombre de pauses borné.

    Une lecture de la file attend par pauses de 0,1 s au plus : quelques dizaines par test au
    maximum. Une boucle d'attente dont la condition de sortie serait fausse enchaînerait au
    contraire des pauses nulles sans fin : au-delà de ``PAUSES_MAX_PAR_TEST``, le test échoue.
    Tout le reste (``monotonic``, ``time``) est le vrai module ``time``.
    """

    def __init__(self) -> None:
        self.pauses = 0

    def __getattr__(self, nom: str) -> Any:
        return getattr(time, nom)

    def sleep(self, secondes: float) -> None:
        self.pauses += 1
        if self.pauses > PAUSES_MAX_PAR_TEST:
            raise AssertionError("boucle d'attente sans fin : trop de pauses dans ce test")
        time.sleep(secondes)


@pytest.fixture(autouse=True)
def pauses_bornees_dans_la_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chaque test part d'un compteur de pauses à zéro (un test peut poser sa propre horloge)."""
    monkeypatch.setattr(file_attente_module, "time", TempsBorne())


@pytest.fixture(autouse=True)
def aucun_courtier_kafka_reel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aucun test ne joint un vrai courtier Kafka.

    Le client réel ``confluent_kafka`` est remplacé par un faux module dont les classes refusent
    d'être créées. Un test qui a besoin d'un client le fournit lui-même (fabrique ou faux
    module) ; un code qui appellerait le client réel par erreur échoue tout de suite au lieu
    d'attendre un courtier absent (jusqu'à 30 s par ``flush``).
    """

    def refuser(*args: Any, **options: Any) -> None:
        raise AssertionError("client Kafka réel appelé pendant un test")

    module = types.ModuleType("confluent_kafka")
    module.Producer = refuser  # type: ignore[attr-defined]
    module.Consumer = refuser  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "confluent_kafka", module)
