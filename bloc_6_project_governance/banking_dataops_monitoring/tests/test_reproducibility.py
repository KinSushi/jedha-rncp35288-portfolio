#!/usr/bin/env python3

"""Reproducibility: an identical input must produce an identical output.

WHY THIS MATTERS MORE THAN IT LOOKS
-----------------------------------
The project claims its results are reproducible. That claim is either mechanical
or worthless. These tests make it mechanical: the same seed produces the same
bytes, and the fingerprint proves it.

It is also the strongest answer to the question "your data is synthetic, what
does that invalidate?" — reproducibility is *superior* to what a real extract
would offer, because anyone can regenerate the exact same dataset and obtain the
exact same verdicts.
"""

from __future__ import annotations

import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from banking_dataops.audit import fingerprint_directory, fingerprint_file
from banking_dataops.generate_synthetic_data import SyntheticConfig, generate

ANCHOR = datetime(2026, 8, 3, 6, 45, tzinfo=UTC)


def _generate_into(directory: Path, seed: int, transactions: int = 120) -> None:
    """Generate a dataset into a directory, with a controlled configuration."""

    generate(
        SyntheticConfig(
            customers=20,
            accounts_per_customer=2,
            transactions=transactions,
            seed=seed,
            generated_at=ANCHOR,
        ),
        directory,
    )


def test_the_same_seed_gives_byte_identical_files_another_seed_does_not_and_a_missing_file_has_no_fingerprint() -> None:
    """The core reproducibility guarantee, and its symmetric check.

    Two runs, same seed, separate directories: every generated file must be byte-identical. Anything less and the
    "deterministic generation" claim in the dossier is unsupported. A generator ignoring its seed would pass that
    perfectly while being useless, so a different seed must give different data. And a generator producing an
    approximate volume would break every count-based control threshold without warning.
    """

    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b, tempfile.TemporaryDirectory() as c:
        dir_a, dir_b, dir_c = Path(a), Path(b), Path(c)
        _generate_into(dir_a, seed=42)
        _generate_into(dir_b, seed=42)
        _generate_into(dir_c, seed=43)
        files_a = sorted(p.name for p in dir_a.glob("*.csv"))
        files_b = sorted(p.name for p in dir_b.glob("*.csv"))
        assert files_a == files_b, "the same seed produced a different set of files"
        assert files_a, "the generator produced nothing"
        for name in files_a:
            assert fingerprint_file(dir_a / name) == fingerprint_file(dir_b / name), (
                f"{name} differs between two runs with seed 42"
            )
        assert fingerprint_directory(dir_a) != fingerprint_directory(dir_c)

        # The volume asked for is the volume written.
        _generate_into(dir_c, seed=42, transactions=137)
        content = (dir_c / "synthetic_transactions.csv").read_text(encoding="utf-8")
        data_rows = len(content.strip().splitlines()) - 1  # minus the header
        assert data_rows == 137

        # FORBIDDEN PATH: a fingerprint of nothing. A file that is not there has no digest, and the error says so.
        with pytest.raises(FileNotFoundError):
            fingerprint_file(dir_a / "absent.csv")


def test_the_directory_fingerprint_depends_on_names_not_on_order_and_the_anchor_removes_the_clock_leak() -> None:
    """A dataset is its content *and* its structure, and the digest must not depend on enumeration order.

    Without sorting, the same dataset would fingerprint differently on two machines, and the reproducibility proof
    would prove nothing. Renaming a file changes the dataset: a digest blind to names would report two different
    datasets as identical. The wall clock is the defect these tests were written to catch, kept as a regression
    guard: without ``generated_at`` the generator reads the clock and writes it into every row, so two runs with
    an identical seed differ. Asserting the difference documents *why* the anchor exists, so nobody removes it
    later believing it is decorative; the anchor removes the leak.
    """

    with tempfile.TemporaryDirectory() as a:
        directory = Path(a)
        _generate_into(directory, seed=7)
        first = fingerprint_directory(directory)
        second = fingerprint_directory(directory)
        assert first == second
        assert re.fullmatch(r"[0-9a-f]{64}", first)  # SHA-256, hexadecimal

    with tempfile.TemporaryDirectory() as a:
        directory = Path(a)
        (directory / "alpha.csv").write_text("id\n1\n", encoding="utf-8")
        before = fingerprint_directory(directory)
        (directory / "alpha.csv").rename(directory / "beta.csv")
        after = fingerprint_directory(directory)
        assert before != after
        # The old name is gone: it has no fingerprint of its own any more.
        with pytest.raises(FileNotFoundError):
            fingerprint_file(directory / "alpha.csv")

    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        dir_a, dir_b = Path(a), Path(b)
        config = SyntheticConfig(customers=5, accounts_per_customer=1, transactions=10, seed=1)
        generate(config, dir_a)
        time.sleep(0.01)  # guarantees a distinct wall clock
        generate(config, dir_b)
        assert fingerprint_directory(dir_a) != fingerprint_directory(dir_b), (
            "the generator became deterministic without an anchor; if that is "
            "intentional, delete this test rather than weakening it"
        )

    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        dir_a, dir_b = Path(a), Path(b)
        anchored = SyntheticConfig(customers=5, accounts_per_customer=1, transactions=10, seed=1, generated_at=ANCHOR)
        generate(anchored, dir_a)
        generate(anchored, dir_b)
        assert fingerprint_directory(dir_a) == fingerprint_directory(dir_b)
