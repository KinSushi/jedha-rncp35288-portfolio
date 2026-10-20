"""The Bronze generator (``generate_synthetic_data``): files, identifiers, windows, the risk rule, refusals.

``tests/test_anchor.py`` proves that the default batch reproduces its logged fingerprint and that the anchor is
parsed and refused correctly. These tests read the generated files and check each documented rule one by one,
so that a changed constant is caught by the rule it breaks and not only by a changed fingerprint.
"""

from __future__ import annotations

import csv
import dataclasses
import random
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from banking_dataops import generate_synthetic_data as G
from banking_dataops.generate_synthetic_data import SyntheticConfig, _write_csv, generate

ANCHOR = datetime(2026, 7, 27, tzinfo=UTC)
CONFIG = SyntheticConfig(customers=12, accounts_per_customer=3, transactions=300, seed=7, generated_at=ANCHOR)

#: The risk weights written as literals: a constant read from the module would move with the code under test.
CHANNEL_RISK = {"api": 0.24, "web": 0.13, "mobile": 0.06, "branch": 0.00}
CATEGORY_RISK = {
    "electronics": 0.17,
    "travel": 0.14,
    "services": 0.08,
    "health": 0.04,
    "grocery": 0.00,
    "utilities": 0.00,
}


def _read(directory: Path, name: str) -> list[dict[str, str]]:
    with (directory / f"synthetic_{name}.csv").open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


@pytest.fixture
def batch(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One batch per test: a shared one would be credited to the first test only by the mutation tool."""
    directory = tmp_path_factory.mktemp("bronze_contract")
    generate(CONFIG, directory)
    return directory


# ---------------------------------------------------------------- the files, the identifiers, the windows


def test_a_batch_is_written_twice_with_stable_bytes_sequential_identifiers_and_the_documented_windows(
    batch: Path, tmp_path: Path
) -> None:
    # Each dataset is written under a stable name and under a stamped name, with the same bytes.
    assert sorted(p.name for p in batch.iterdir()) == [
        "accounts_20260727_000000.csv",
        "customers_20260727_000000.csv",
        "synthetic_accounts.csv",
        "synthetic_customers.csv",
        "synthetic_transactions.csv",
        "transactions_20260727_000000.csv",
    ]
    for name in ("accounts", "customers", "transactions"):
        assert (batch / f"synthetic_{name}.csv").read_bytes() == (batch / f"{name}_20260727_000000.csv").read_bytes()

    # The output folder is created, the same config gives the same bytes, another seed gives other transactions.
    generate(CONFIG, tmp_path / "a" / "b")
    generate(dataclasses.replace(CONFIG, seed=8), tmp_path / "eight")
    for name in ("customers", "accounts", "transactions"):
        assert (tmp_path / "a" / "b" / f"synthetic_{name}.csv").read_bytes() == (
            batch / f"synthetic_{name}.csv"
        ).read_bytes()
    assert (tmp_path / "eight" / "synthetic_transactions.csv").read_bytes() != (
        batch / "synthetic_transactions.csv"
    ).read_bytes()

    # The csv module's line terminator is kept, so that the bytes do not depend on the platform.
    raw = (batch / "synthetic_customers.csv").read_bytes()
    assert raw.startswith(b"customer_id,customer_segment,country,created_at\r\n")
    assert raw.count(b"\r\n") == raw.count(b"\n")

    # Customers: sequential identifiers, known segments and countries, created 120 to 30 days before the anchor.
    customers = _read(batch, "customers")
    assert [c["customer_id"] for c in customers] == [f"CUST-{i:05d}" for i in range(1, 13)]
    assert {c["customer_segment"] for c in customers} <= {"retail", "premium", "sme"}
    assert {c["country"] for c in customers} <= {"CH", "FR", "DE", "IT", "AT"}
    created_by_customer = {c["customer_id"]: datetime.fromisoformat(c["created_at"]) for c in customers}
    for created in created_by_customer.values():
        assert ANCHOR - timedelta(days=120) <= created <= ANCHOR - timedelta(days=30)

    # Accounts: the configured number per customer, opened within twenty days of the customer's creation.
    accounts = _read(batch, "accounts")
    assert [a["account_id"] for a in accounts][:4] == ["ACC-00001-01", "ACC-00001-02", "ACC-00001-03", "ACC-00002-01"]
    assert len(accounts) == len(customers) * 3
    assert {a["account_type"] for a in accounts} <= {"current", "savings"}
    assert {a["currency"] for a in accounts} == {"CHF"}
    for account in accounts:
        opened = datetime.fromisoformat(account["opened_at"])
        created = created_by_customer[account["customer_id"]]
        assert created <= opened <= created + timedelta(days=20)
        assert account["account_id"].startswith(f"ACC-{account['customer_id'].removeprefix('CUST-')}-")

    # Transactions: the thirty days before the anchor, stamped with the anchor, on existing accounts.
    transactions = _read(batch, "transactions")
    assert [t["transaction_id"] for t in transactions] == [f"TX-{i:08d}" for i in range(1, 301)]
    for transaction in transactions:
        stamp = datetime.fromisoformat(transaction["event_timestamp"])
        assert ANCHOR - timedelta(days=30) <= stamp <= ANCHOR
        assert transaction["booking_date"] == stamp.date().isoformat()
        assert transaction["created_at"] == ANCHOR.isoformat()
        assert transaction["currency"] == "CHF"
    assert {t["account_id"] for t in transactions} <= {a["account_id"] for a in accounts}
    assert {t["status"] for t in transactions} <= {"posted", "pending", "rejected"}
    assert {t["channel"] for t in transactions} <= set(CHANNEL_RISK)
    assert {t["merchant_category"] for t in transactions} <= set(CATEGORY_RISK)
    assert {t["source_system"] for t in transactions} <= {"core_banking", "card_processor", "payments_gateway"}
    assert {t["country"] for t in transactions} <= {"CH", "FR", "DE", "IT", "AT"}


def test_one_transaction_in_137_is_large_and_the_risk_score_follows_the_documented_rule_within_the_noise_band(
    batch: Path,
) -> None:
    """Night 0.19, cross-border 0.16, high amount 0.21, the channel and category weights, noise below 0.28."""
    country = {c["customer_id"]: c["country"] for c in _read(batch, "customers")}
    owner = {a["account_id"]: a["customer_id"] for a in _read(batch, "accounts")}
    transactions = _read(batch, "transactions")

    large = [float(t["amount_chf"]) for i, t in enumerate(transactions) if i % 137 == 0]
    others = [float(t["amount_chf"]) for i, t in enumerate(transactions) if i % 137 != 0]
    assert [i for i in range(len(transactions)) if i % 137 == 0] == [0, 137, 274]
    assert len(large) == len(range(0, 300, 137))
    assert all(50_000 <= amount <= 125_000 for amount in large)
    assert max(others) < 50_000
    assert min(others) > 0
    assert all(round(float(t["amount_chf"]), 2) == float(t["amount_chf"]) for t in transactions)

    for transaction in transactions:
        night = datetime.fromisoformat(transaction["event_timestamp"]).hour < 6
        cross_border = transaction["country"] != country[owner[transaction["account_id"]]]
        high = float(transaction["amount_chf"]) > 10_000
        base = (
            0.19 * night
            + 0.16 * cross_border
            + 0.21 * high
            + CHANNEL_RISK[transaction["channel"]]
            + CATEGORY_RISK[transaction["merchant_category"]]
        )
        score = float(transaction["risk_score"])
        assert min(0.9999, base) - 1e-4 <= score <= min(0.9999, base + 0.28) + 1e-4, transaction["transaction_id"]
        assert 0.0 <= score <= 0.9999
        assert (transaction["is_suspicious"] == "True") is (score > 0.75)
    assert {t["is_suspicious"] for t in transactions} == {"True", "False"}


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("overrides", "error", "message", "absent"),
    [
        ({"customers": 0}, IndexError, "Cannot choose from an empty sequence", None),
        ({"accounts_per_customer": 0}, IndexError, "Cannot choose from an empty sequence", None),
        ({"transactions": 0}, ValueError, "No rows to write for .*synthetic_transactions.csv", "transactions"),
        ({"transactions": -5}, ValueError, "No rows to write for .*synthetic_transactions.csv", "transactions"),
    ],
)
def test_a_generation_without_an_account_or_without_a_transaction_is_refused_and_writes_no_such_file(
    tmp_path: Path, overrides: dict[str, int], error: type[Exception], message: str, absent: str | None
) -> None:
    """FORBIDDEN PATH: transactions with no account (the load would refuse the foreign key), or a header-only file
    that cleaning would take for a valid empty batch. No account: the standard library's ``IndexError``, raised
    before any file is written."""
    with pytest.raises(error, match=message):
        generate(dataclasses.replace(CONFIG, **overrides), tmp_path / "out")

    if absent is None:
        assert list((tmp_path / "out").glob("*.csv")) == []
    else:
        assert not (tmp_path / "out" / f"synthetic_{absent}.csv").exists()
        assert not list((tmp_path / "out").glob(f"{absent}_*.csv"))


def test_a_csv_is_written_with_the_columns_of_its_first_row_in_utf_8_and_an_empty_list_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_csv(tmp_path / "deep" / "out.csv", [{"b": "Zürich", "a": 1}, {"b": "Genève", "a": 2}])
    assert (tmp_path / "deep" / "out.csv").read_bytes() == "b,a\r\nZürich,1\r\nGenève,2\r\n".encode()
    _write_csv(tmp_path / "one.csv", [{"b": 2, "a": 1}])
    assert (tmp_path / "one.csv").read_bytes() == b"b,a\r\n2,1\r\n"

    # Without ``newline=""`` Windows would double the line terminators and the bytes would differ by platform.
    opened: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    original = Path.open

    def spy(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append((args, kwargs))
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", spy)
        _write_csv(tmp_path / "x.csv", [{"a": 1}])
    assert opened == [(("w",), {"newline": "", "encoding": "utf-8"})]

    target = tmp_path / "nested" / "empty.csv"
    with pytest.raises(ValueError) as raised:
        _write_csv(target, [])
    assert str(raised.value) == f"No rows to write for {target}"
    assert not target.exists()
    assert target.parent.is_dir()


def test_a_config_is_frozen_and_the_command_line_has_the_documented_defaults_and_refuses_what_it_cannot_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        CONFIG.seed = 1  # type: ignore[misc]
    config = SyntheticConfig()
    assert (config.customers, config.accounts_per_customer, config.transactions, config.seed) == (100, 2, 1500, 42)
    assert config.generated_at is None

    args = G.parse_args([])
    assert (args.output_dir, args.customers, args.accounts_per_customer, args.transactions, args.seed) == (
        "data",
        100,
        2,
        1500,
        42,
    )
    assert args.generated_at is None

    for argv in (
        ["--customers", "many"],
        ["--seed", "4.2"],
        ["--transactions"],
        ["--unknown-option"],
        ["--accounts-per-customer", ""],
    ):
        with pytest.raises(SystemExit) as raised:
            G.parse_args(argv)
        assert raised.value.code == 2
        assert "usage:" in capsys.readouterr().err


# ---------------------------------------------------------------- the command line, the clock, the thresholds


def test_main_writes_the_batch_it_is_asked_for_prints_one_summary_line_and_shows_the_documented_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(G.GENERATED_AT_ENV, raising=False)

    G.main(
        [
            "--output-dir", str(tmp_path / "out"),
            "--customers", "4",
            "--accounts-per-customer", "2",
            "--transactions", "20",
            "--seed", "3",
            "--generated-at", "2026-07-27T12:30:00+02:00",
        ]
    )  # fmt: skip
    assert capsys.readouterr().out == (
        "Generated synthetic data: 4 customers, 8 accounts, 20 transactions (anchor 2026-07-27T10:30:00Z)\n"
    )
    assert _read(tmp_path / "out", "transactions")[-1]["transaction_id"] == "TX-00000020"
    assert (tmp_path / "out" / "synthetic_transactions.csv").is_file()
    assert (tmp_path / "out" / "transactions_20260727_103000.csv").is_file()

    # The seed and the accounts per customer reach the generator: the files equal those of a direct call.
    expected = SyntheticConfig(
        customers=4, accounts_per_customer=3, transactions=20, seed=3, generated_at=datetime(2026, 7, 27, tzinfo=UTC)
    )
    generate(expected, tmp_path / "direct")
    G.main(
        [
            "--output-dir", str(tmp_path / "cli"),
            "--customers", "4",
            "--accounts-per-customer", "3",
            "--transactions", "20",
            "--seed", "3",
        ]
    )  # fmt: skip
    for name in ("customers", "accounts", "transactions"):
        assert (tmp_path / "cli" / f"synthetic_{name}.csv").read_bytes() == (
            tmp_path / "direct" / f"synthetic_{name}.csv"
        ).read_bytes()
    assert "4 customers, 12 accounts, 20 transactions" in capsys.readouterr().out

    # The help is what an operator reads: it is compared with ``tests/golden/generate_help.txt``.
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setattr(sys, "argv", ["generate_synthetic_data"])
    with pytest.raises(SystemExit) as exited:
        G.parse_args(["--help"])
    assert exited.value.code == 0
    golden = (Path(__file__).parent / "golden" / "generate_help.txt").read_text(encoding="utf-8")
    assert capsys.readouterr().out == golden


class _ZoneMinusFiveClock(datetime):
    """A clock on a machine set to UTC-5: ``now()`` without a zone is the local time, ``now(UTC)`` the real one."""

    @classmethod
    def now(cls, tz: Any = None) -> Any:
        instant = datetime(2026, 7, 27, 14, 14, 5, tzinfo=UTC)
        if tz is None:
            return instant.astimezone(timezone(timedelta(hours=-5))).replace(tzinfo=None)
        return instant.astimezone(tz)


class _PinnedLargeAmount(random.Random):
    """The generator's own random source, except that the large-amount draw is pinned and consumes nothing."""

    amount = 0.0

    def uniform(self, a: float, b: float) -> float:
        return self.amount


def _risk_of_the_first_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, amount: float) -> float:
    directory = tmp_path / f"amount_{amount}"
    with monkeypatch.context() as patch:
        patch.setattr(G.random, "Random", type("Pinned", (_PinnedLargeAmount,), {"amount": amount}))
        generate(CONFIG, directory)

    first = _read(directory, "transactions")[0]
    assert float(first["amount_chf"]) == amount
    return float(first["risk_score"])


def test_without_an_anchor_the_clock_is_read_once_in_utc_and_the_thresholds_are_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(G, "datetime", _ZoneMinusFiveClock)
        generate(dataclasses.replace(CONFIG, generated_at=None), tmp_path / "clock")
    assert {t["created_at"] for t in _read(tmp_path / "clock", "transactions")} == {"2026-07-27T14:14:05+00:00"}
    assert (tmp_path / "clock" / "transactions_20260727_141405.csv").is_file()

    # Same random draws, one centime apart: the risk score differs by the weight of the high-amount factor (0.21).
    at_the_limit = _risk_of_the_first_transaction(tmp_path, monkeypatch, 10_000.0)
    just_above = _risk_of_the_first_transaction(tmp_path, monkeypatch, 10_000.01)
    assert just_above < 0.9999
    assert just_above - at_the_limit == pytest.approx(0.21, abs=1e-4)

    # Seed 52 of this small batch draws TX-00000186 at exactly 0.75: strictly above is the rule.
    config = SyntheticConfig(customers=20, accounts_per_customer=2, transactions=300, seed=52, generated_at=ANCHOR)
    generate(config, tmp_path / "threshold")
    by_id = {t["transaction_id"]: t for t in _read(tmp_path / "threshold", "transactions")}
    assert float(by_id["TX-00000186"]["risk_score"]) == 0.75
    assert by_id["TX-00000186"]["is_suspicious"] == "False"
