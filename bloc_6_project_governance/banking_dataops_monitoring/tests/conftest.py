"""Shared fixtures for the model, prediction and anchor tests.

They build a small modelling frame from the project's own generator (so the tests
exercise the real label rule, not a hand-made one) and keep the tests away from
PostgreSQL and from the real ``models/`` directory.
"""

from __future__ import annotations

import copy
import logging
import signal
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import psycopg
import pytest
from fakes import FakeDatabase

from banking_dataops import audit
from banking_dataops.config import Settings
from banking_dataops.generate_synthetic_data import DEFAULT_GENERATED_AT, SyntheticConfig, generate
from banking_dataops.ml import features as F
from banking_dataops.ml import predict as P
from banking_dataops.ml import train as T

#: Seconds after which a test is failed instead of left to run: a loop whose index no longer advances would
#: otherwise hang the suite (and be counted as a time-out, not as a detection, by the mutation tool, whose own
#: limit is about fifteen seconds for fast tests). A heavy test says so with ``@pytest.mark.limit(seconds)``.
DEFAULT_TIME_LIMIT_S = 8

#: Columns of the SQL extract in banking_dataops/ml/features.py, in the same order.
EXTRACT_COLUMNS = [
    "transaction_id",
    "amount_chf",
    "channel",
    "merchant_category",
    "transaction_country",
    "source_system",
    "event_timestamp",
    "is_suspicious",
    "customer_segment",
    "customer_country",
]


@pytest.fixture(scope="session")
def _generated_frame(tmp_path_factory: pytest.TempPathFactory) -> pd.DataFrame:
    """Generate one 800-transaction batch and join it the way ``features._EXTRACT_SQL`` does."""
    directory = tmp_path_factory.mktemp("bronze")
    generate(
        SyntheticConfig(
            customers=60,
            accounts_per_customer=2,
            transactions=800,
            seed=42,
            generated_at=DEFAULT_GENERATED_AT,
        ),
        directory,
    )
    customers = pd.read_csv(directory / "synthetic_customers.csv")
    accounts = pd.read_csv(directory / "synthetic_accounts.csv")
    transactions = pd.read_csv(directory / "synthetic_transactions.csv")
    joined = (
        transactions.merge(accounts[["account_id", "customer_id"]], on="account_id")
        .merge(
            customers[["customer_id", "customer_segment", "country"]].rename(columns={"country": "customer_country"}),
            on="customer_id",
        )
        .rename(columns={"country": "transaction_country"})
    )
    joined = joined[joined["source_system"] != "demo_scenario"].sort_values("transaction_id")
    return joined[EXTRACT_COLUMNS].reset_index(drop=True)


@pytest.fixture
def raw_frame(_generated_frame: pd.DataFrame) -> pd.DataFrame:
    """A private copy of the raw frame: tests may corrupt it freely."""
    return _generated_frame.copy()


@pytest.fixture
def audit_log(monkeypatch: pytest.MonkeyPatch) -> list[tuple[audit.AuditEvent, object]]:
    """Capture every audit event with the settings it was meant to be persisted with.

    Nothing reaches PostgreSQL. A test reads the list to check what the code journalled: the
    event, its reason, its figures, and whether the settings it was given went all the way down.
    """
    captured: list[tuple[audit.AuditEvent, object]] = []

    def _capture(event: audit.AuditEvent, settings: object = None) -> bool:
        captured.append((event, settings))
        return False

    monkeypatch.setattr(audit, "_persist_event", _capture)
    return captured


@pytest.fixture
def no_audit_database(audit_log: list[tuple[audit.AuditEvent, object]]) -> None:
    """Keep audit events in the log only: no test may write to a PostgreSQL that happens to run."""


@pytest.fixture
def small_forest_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the forest grid from 12 combinations to 2 so training tests stay fast."""
    monkeypatch.setattr(
        T,
        "FOREST_PARAM_GRID",
        {"model__n_estimators": [15, 30], "model__max_depth": [4], "model__min_samples_leaf": [1]},
    )


@pytest.fixture
def model_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the model artefact and its metrics to a temporary directory."""
    directory = tmp_path / "models"
    monkeypatch.setattr(T, "ROOT", tmp_path)
    monkeypatch.setattr(T, "MODEL_DIR", directory)
    monkeypatch.setattr(T, "MODEL_PATH", directory / "suspicion_classifier.joblib")
    monkeypatch.setattr(T, "METRICS_PATH", directory / "metrics.json")
    monkeypatch.setattr(P, "MODEL_PATH", directory / "suspicion_classifier.joblib")
    return directory


@pytest.fixture
def extract_returns(monkeypatch: pytest.MonkeyPatch):
    """Return a setter that makes ``features.extract`` answer with a given frame."""

    def _set(frame: pd.DataFrame) -> None:
        monkeypatch.setattr(F, "extract", lambda settings=None: frame)

    return _set


@pytest.fixture
def fake_database(monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    """Replace ``psycopg.connect`` with a recording fake: nothing ever reaches a real PostgreSQL.

    The returned object lists the connection strings, the statements with their parameters, the
    commits and the ``COPY`` payloads, and answers each statement from a script (``tests/fakes.py``).
    """
    database = FakeDatabase()
    monkeypatch.setattr(psycopg, "connect", database.connect)
    return database


@pytest.fixture
def settings() -> Settings:
    """Settings that cannot come from the environment or from a ``.env`` file of the developer."""
    return Settings(
        _env_file=None,
        database_host="db.test",
        database_port=6543,
        database_name="bank",
        database_user="alice",
        database_password="s3cret",
    )


class _ListHandler(logging.Handler):
    """Collects the records of the project logger, which does not propagate to pytest's ``caplog``."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        # A copy: the correlation filter of the console handler adds ``run_id`` and ``step`` to the very same
        # record afterwards, and the test must see the record as the code built it.
        self.records.append(copy.copy(record))


@pytest.fixture
def log_records() -> Iterator[list[logging.LogRecord]]:
    """Every record the ``banking_dataops`` loggers emit during the test, DEBUG included.

    ``configure_logging`` sets ``propagate = False`` on the project logger, so ``caplog`` sees nothing:
    the handler is attached to the project logger itself, and the previous level is restored afterwards.
    """
    logger = logging.getLogger("banking_dataops")
    handler = _ListHandler()
    previous_level = logger.level
    # First in the list: the handler sees the record as the code built it, before the correlation filter of
    # the console handler adds ``run_id`` and ``step`` to it.
    logger.handlers.insert(0, handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


@pytest.fixture(autouse=True)
def _no_test_hangs(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail a test that exceeds its time limit (8 s unless marked ``limit``) instead of letting it run for ever.

    ``SIGALRM`` exists on Linux and macOS only: on Windows the fixture does nothing. The alarm interrupts a pure
    Python loop between two bytecodes, so a splitter or a pager whose index stopped advancing ends as a failure.
    """
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    marker = request.node.get_closest_marker("limit")
    limit = float(marker.args[0]) if marker else DEFAULT_TIME_LIMIT_S

    def on_alarm(signum: int, frame: object) -> None:
        raise TimeoutError(f"the test ran longer than {limit} s: a loop no longer ends")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, limit)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
