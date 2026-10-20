#!/usr/bin/env python3
"""Generate the synthetic Bronze layer: customers, accounts and transactions as CSV.

The data are fake and safe to publish. They are generated, not sampled, so the
generator can plant the risk factors the classifier is later asked to recover, and
so that anyone can reproduce the exact same rows from a seed and a date anchor.

CHOIX
-----
Retained:

- Synthetic data, one fixed seed (42) and one explicit date anchor
  (``generated_at``). Every timestamp is derived from the anchor, never from the
  wall clock, so two runs with the same seed and the same anchor write the same
  bytes (ADR-0003, completed by ADR-0009).
- A reference anchor, ``DEFAULT_GENERATED_AT`` = 2026-07-27T00:00:00Z, used by both
  command-line entry points when nothing else is given. It is the anchor that
  reproduces the batch fingerprint ``89ac138fb9943f00`` of evidence/pipeline_full.txt.
  It can be overridden by ``--generated-at`` or by the environment variable
  ``BANKING_DATAOPS_GENERATED_AT``; ``today`` restores the earlier "midnight UTC of
  the current day" behaviour, which is not reproducible from one day to the next.
- A label that is a documented rule (``risk_score > 0.75``) over observable factors
  plus noise, so that a model has something real to learn without ever seeing
  ``risk_score``.
- Two copies of each file: a stable name for the pipeline and a timestamped archive,
  so a verdict can be traced to the extract that produced it.

Rejected:

- Anchoring on ``datetime.now()`` inside the generator, the earlier behaviour:
  ``weekday_name`` and ``is_weekend`` are derived from the timestamps, so the model
  inputs, its AUC and its threshold changed with the day of execution (ADR-0009).
- A random ``risk_score``: the label then carried no learnable structure, and a model
  could reach only chance level, or a perfect score if it was given the column
  (ADR-0008, "Problème résolu en chemin").
- A real or anonymised extract: no legitimate access (ADR-0003).

OÙ
---
Role: the Bronze step of the pipeline. Callers: ``run_pipeline.py`` (step ``bronze``),
``docker/entrypoint.sh`` and the Makefile targets ``generate`` and ``reset`` through
``python -m banking_dataops.generate_synthetic_data``, the alias
``src/ingestion/ingest.py``, and the tests of reproducibility. Depends on the standard
library only: no database, no secrets. Output: ``data/synthetic_*.csv`` and the
timestamped archives, read by ``banking_dataops.pipeline.cleaning`` and
``banking_dataops.ingest``.

COMMENT
-------
``generate`` seeds one ``random.Random`` and draws in a fixed order: customers and
their accounts first, then transactions. The order of the draws is part of the
contract: inserting a draw anywhere changes every later row. Each transaction gets a
``risk_score`` that is the sum of its risk factors, a noise term and a clamp to
[0, 0.9999]; ``is_suspicious`` is ``risk_score > 0.75``. ``resolve_anchor`` turns the
command-line value, the environment variable or the default into a timezone-aware UTC
datetime.
Tests that prove it: tests/test_reproducibility.py (same seed and anchor give the same
bytes; no anchor does not), tests/test_synthetic_data.py, and tests/test_anchor.py
(precedence and refusals of the anchor).

POURQUOI
--------
- The default anchor is the only date among 911 tested (1 January 2025 to 30 June
  2027) that reproduces the logged fingerprint (evidence/rejeu_modele_2026-07-27.md,
  "Pourquoi le 27 juillet 2026").
- One clock read per run, taken from the anchor: three reads could straddle midnight
  and produce accounts created after their own transactions (docs/CODE_STANDARD.md,
  section 4).
- Risk weights and the 0.75 cut-off are project choices that mimic an AML scoring
  engine: night activity, cross-border, high amount, channel, merchant category and
  noise (ADR-0008). Source for the numeric values: inconnue, they are not taken from a
  regulatory text.
- About one transaction in 137 is a large amount (50,000 to 125,000 CHF), so that the
  high-amount factor has positives: 11 rows in 1,500 (evidence/rejeu_modele_2026-07-27.json,
  ``montants_50k_et_plus``). They stay under the 1,000,000 CHF ceiling of CTRL-003
  (src/banking_dataops/quality_checks.py), so the generator never breaks a control by
  itself; the anomalies used to exercise the controls come from ``demo_scenario``.

JOURNALISATION
--------------
Nothing is logged here: this module is a pure generator over a seeded random source and
writes only the CSV files. The caller records the event: ``run_pipeline.step_bronze``
writes ``bronze_generated`` (category ``data``) with the seed, the anchor and the
SHA-256 fingerprint of the files. ``main()`` prints one summary line.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: Reference anchor of every generated batch. Midnight UTC, 27 July 2026: the date whose
#: batch fingerprint (89ac138fb9943f00) is logged in evidence/pipeline_full.txt.
DEFAULT_GENERATED_AT = datetime(2026, 7, 27, tzinfo=UTC)

#: Environment variable that overrides the default anchor. The prefix is the one every
#: other setting of the project uses (see config.py).
GENERATED_AT_ENV = "BANKING_DATAOPS_GENERATED_AT"

#: Keyword for "midnight UTC of the current day". Not reproducible from one day to the
#: next: the weekday of the anchor changes two model inputs.
ANCHOR_TODAY = "today"


@dataclass(frozen=True)
class SyntheticConfig:
    """Immutable parameters of one generation.

    Frozen so that nothing can change the seed or the anchor halfway through a run.

    ``generated_at`` is the temporal anchor of every timestamp. The default ``None``
    reads the clock once and is therefore NOT reproducible; it is kept for library
    callers and locked by a test that shows the defect it causes. The command-line
    entry points never use it: they resolve an explicit anchor with ``resolve_anchor``.
    """

    customers: int = 100
    accounts_per_customer: int = 2
    transactions: int = 1500
    seed: int = 42
    generated_at: datetime | None = None


def parse_anchor(text: str) -> datetime:
    """Parse an anchor given as ISO 8601 text, or the keyword ``today``.

    A value without a time zone is read as UTC, and any offset is converted to UTC, so
    that two spellings of the same instant give the same bytes. A value that is not a
    date raises ``ValueError`` naming the accepted forms: silently falling back to the
    clock would hide exactly the non-reproducibility this anchor exists to remove.
    """
    value = text.strip()
    if value.lower() == ANCHOR_TODAY:
        return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid generation anchor {text!r}: expected ISO 8601 such as "
            f"'2026-07-27T00:00:00Z' or '2026-07-27', or the keyword '{ANCHOR_TODAY}'."
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def resolve_anchor(cli_value: str | None = None, environ: Mapping[str, str] | None = None) -> datetime:
    """Return the anchor to use: the command line, else the environment, else the default.

    Precedence, strongest first: ``--generated-at``, ``BANKING_DATAOPS_GENERATED_AT``,
    ``DEFAULT_GENERATED_AT``. An empty value counts as absent, so that an unset
    ``${VAR:-}`` in a compose file does not turn into an error. A non-empty invalid value
    raises: the stronger source is never ignored in favour of a weaker one.
    """
    if cli_value:
        return parse_anchor(cli_value)
    env = os.environ if environ is None else environ
    from_env = env.get(GENERATED_AT_ENV, "").strip()
    if from_env:
        return parse_anchor(from_env)
    return DEFAULT_GENERATED_AT


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    """Write dictionaries as a UTF-8 CSV file, creating the parent directory.

    An empty list raises instead of writing a header-only file: a Bronze file with no
    row is a bug upstream, and a header-only CSV would flow through cleaning as a valid
    empty batch.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError(f"No rows to write for {path}")
    # newline="" because csv writes its own line terminators; without it Windows
    # would double them and the bytes would differ between platforms.
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def generate(config: SyntheticConfig, output_dir: Path) -> None:
    """Generate customers, accounts and transactions and write them as CSV files.

    Every timestamp is derived from ``config.generated_at`` (or from the clock, read
    once, when it is ``None``). The sequence of random draws is fixed: do not insert a
    draw without accepting that every following row changes.
    """
    rng = random.Random(config.seed)
    anchor = config.generated_at or datetime.now(UTC)

    customer_segments = ["retail", "premium", "sme"]
    countries = ["CH", "FR", "DE", "IT", "AT"]
    channels = ["web", "mobile", "branch", "api"]
    categories = ["grocery", "travel", "health", "utilities", "electronics", "services"]
    source_systems = ["core_banking", "card_processor", "payments_gateway"]

    customers: list[dict[str, object]] = []
    accounts: list[dict[str, object]] = []
    # Customers are created 120 to 30 days before the anchor and their accounts up to 20
    # days later; the transactions fall in the 30 days before the anchor.
    created_anchor = anchor - timedelta(days=120)

    for index in range(config.customers):
        customer_id = f"CUST-{index + 1:05d}"
        customer_created = created_anchor + timedelta(days=rng.randint(0, 90))
        customers.append(
            {
                "customer_id": customer_id,
                "customer_segment": rng.choice(customer_segments),
                "country": rng.choice(countries),
                "created_at": customer_created.isoformat(),
            }
        )

        for account_index in range(config.accounts_per_customer):
            accounts.append(
                {
                    "account_id": f"ACC-{index + 1:05d}-{account_index + 1:02d}",
                    "customer_id": customer_id,
                    "account_type": rng.choice(["current", "savings"]),
                    "currency": "CHF",
                    "opened_at": (customer_created + timedelta(days=rng.randint(0, 20))).isoformat(),
                }
            )

    customer_country = {c["customer_id"]: c["country"] for c in customers}
    account_owner = {a["account_id"]: a["customer_id"] for a in accounts}

    # Risk weights. The first generator drew risk_score at random, so the label held no
    # structure: a model could reach chance level without the column, or a perfect score
    # with it (a target leak). These weights plant a documented, business-plausible
    # signal that the model must recover from observable features. On real data the label
    # would come from confirmed investigations; here it comes from this rule, and the
    # dossier says so. Values are project choices (see the module docstring).
    CHANNEL_RISK = {"api": 0.24, "web": 0.13, "mobile": 0.06, "branch": 0.00}
    CATEGORY_RISK = {
        "electronics": 0.17,
        "travel": 0.14,
        "services": 0.08,
        "health": 0.04,
        "grocery": 0.00,
        "utilities": 0.00,
    }
    NIGHT_RISK = 0.19  # booked before 06:00
    CROSS_BORDER_RISK = 0.16  # transaction country differs from the customer's country
    HIGH_AMOUNT_RISK = 0.21  # above CHF 10,000
    NOISE_WEIGHT = 0.28  # irreducible noise: keeps the best possible AUC below 1
    # risk_score > 0.75 marks a transaction suspicious. With these weights it selects
    # about 8 % of the rows (121 of 1,500 on the reference anchor).
    SUSPICIOUS_THRESHOLD = 0.75

    transactions: list[dict[str, object]] = []
    base_time = anchor - timedelta(days=30)

    for index in range(config.transactions):
        account = rng.choice(accounts)
        timestamp = base_time + timedelta(minutes=rng.randint(0, 30 * 24 * 60))
        amount = round(rng.lognormvariate(4.0, 0.8), 2)
        # One transaction in 137 is a large amount, so the high-amount factor has positives.
        if index % 137 == 0:
            amount = round(rng.uniform(50000, 125000), 2)

        channel = rng.choice(channels)
        category = rng.choice(categories)
        country = rng.choice(countries)

        owner = account_owner[account["account_id"]]
        is_night = timestamp.hour < 6
        is_cross_border = country != customer_country[owner]
        is_high_amount = amount > 10_000

        risk = (
            NIGHT_RISK * is_night
            + CROSS_BORDER_RISK * is_cross_border
            + HIGH_AMOUNT_RISK * is_high_amount
            + CHANNEL_RISK[channel]
            + CATEGORY_RISK[category]
            + NOISE_WEIGHT * rng.random()
        )
        # CTRL-005 requires a score in [0, 1]; the clamp keeps the generator inside it.
        risk_score = round(min(0.9999, max(0.0, risk)), 4)
        status = rng.choices(["posted", "pending", "rejected"], weights=[86, 9, 5])[0]

        transactions.append(
            {
                "transaction_id": f"TX-{index + 1:08d}",
                "account_id": account["account_id"],
                "source_system": rng.choice(source_systems),
                "event_timestamp": timestamp.isoformat(),
                "booking_date": timestamp.date().isoformat(),
                "amount_chf": amount,
                "currency": "CHF",
                "channel": channel,
                "merchant_category": category,
                "country": country,
                "risk_score": risk_score,
                "status": status,
                "is_suspicious": risk_score > SUSPICIOUS_THRESHOLD,
                "created_at": anchor.isoformat(),
            }
        )

    # A raw layer keeps every ingestion instead of overwriting the previous one, otherwise
    # "which extract produced this verdict?" has no answer and a past batch cannot be
    # replayed. The stable name gives later steps a fixed entry point.
    stamp = anchor.strftime("%Y%m%d_%H%M%S")
    for name, rows in (
        ("customers", customers),
        ("accounts", accounts),
        ("transactions", transactions),
    ):
        _write_csv(output_dir / f"synthetic_{name}.csv", rows)
        _write_csv(output_dir / f"{name}_{stamp}.csv", rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line. ``argv`` is injectable so the parser can be tested."""
    parser = argparse.ArgumentParser(description="Generate synthetic regulated-data CSV files.")
    parser.add_argument("--output-dir", default="data", help="Output directory for CSV files")
    parser.add_argument("--customers", type=int, default=100, help="Number of customers to generate")
    parser.add_argument("--accounts-per-customer", type=int, default=2, help="Number of accounts per customer")
    parser.add_argument("--transactions", type=int, default=1500, help="Number of transactions to generate")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument(
        "--generated-at",
        default=None,
        help=(
            "Date anchor of every timestamp, ISO 8601 (e.g. 2026-07-27T00:00:00Z) or 'today'. "
            f"Default: ${GENERATED_AT_ENV}, else {DEFAULT_GENERATED_AT:%Y-%m-%dT%H:%M:%SZ} "
            "(reproduces the logged batch fingerprint)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point: resolve the anchor, generate, print one summary line."""
    args = parse_args(argv)
    config = SyntheticConfig(
        customers=args.customers,
        accounts_per_customer=args.accounts_per_customer,
        transactions=args.transactions,
        seed=args.seed,
        generated_at=resolve_anchor(args.generated_at),
    )
    generate(config, Path(args.output_dir))
    print(
        "Generated synthetic data: "
        f"{config.customers} customers, "
        f"{config.customers * config.accounts_per_customer} accounts, "
        f"{config.transactions} transactions "
        f"(anchor {config.generated_at:%Y-%m-%dT%H:%M:%SZ})"
    )


if __name__ == "__main__":
    main()
