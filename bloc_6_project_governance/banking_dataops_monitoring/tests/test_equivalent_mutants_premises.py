"""The premises on which the mutants listed in ``MUTANTS_EQUIVALENTS.md`` are called equivalent.

A mutant is equivalent when no test can tell it from the original because the two behave identically. For
most of them the reason is a fact about a library or about Python (a default value, a codec alias, the
truthiness of ``None``). Each such fact is checked here, so that the claim in the document does not rest on
memory: if a library upgrade ever changes one of them, this test fails and the mutant has to be looked at again.
"""

from __future__ import annotations

import argparse
import io
import json
import typing
from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest
from sklearn.compose import ColumnTransformer
from sklearn.metrics import confusion_matrix

from banking_dataops.pipeline.cleaning import CleaningReport
from banking_dataops.quality_checks import QualityCheck


def _factor_three_keeps_the_best_point() -> bool:
    rng = np.random.default_rng(0)
    for _ in range(200):
        precision, recall = rng.random(30), rng.random(30)
        denominator = precision + recall
        two = np.divide(2 * precision * recall, denominator, out=np.zeros_like(precision), where=denominator > 0)
        three = np.divide(3 * precision * recall, denominator, out=np.zeros_like(precision), where=denominator > 0)
        if int(np.argmax(two)) != int(np.argmax(three)):
            return False
    return True


def _falsy_values_fall_back_alike() -> bool:
    absent: object = None
    empty: object = ""
    return (absent or "-") == (empty or "-")


def _csv_text(index: object) -> str:
    buffer = io.StringIO()
    pd.DataFrame({"a": [1, 2]}).to_csv(buffer, index=index)  # type: ignore[arg-type]
    return buffer.getvalue()


#: premise -> a check that returns True when the fact still holds. One row per family of equivalent mutants.
PREMISES: dict[str, Callable[[], bool]] = {
    "sklearn: ColumnTransformer drops the unlisted columns by default": (
        lambda: ColumnTransformer(transformers=[]).remainder == "drop"
    ),
    "numpy: a positive factor does not move the argmax of the F1 curve": _factor_three_keeps_the_best_point,
    "sklearn: a 2x2 confusion matrix has four cells even for a single class": (
        lambda: confusion_matrix([False, False], [False, False], labels=[False, True]).ravel().size == 4
    ),
    "python: rounding to four or five decimals a value already rounded to three changes nothing": (
        lambda: all(round(round(x / 7, 3), n) == round(x / 7, 3) for x in range(1, 500) for n in (4, 5))
    ),
    "pandas: the orient argument of to_dict is case-insensitive": (
        lambda: pd.DataFrame({"a": [1]}).to_dict(orient="RECORDS") == pd.DataFrame({"a": [1]}).to_dict(orient="records")
    ),
    "pandas: to_csv(index=None) writes no index, like index=False": lambda: _csv_text(None) == _csv_text(False),
    "pandas: drop_duplicates keeps the first row by default": (
        lambda: pd.DataFrame({"k": [1, 1], "v": [10, 20]}).duplicated(subset=["k"]).tolist() == [False, True]
    ),
    "python: codec names are case-insensitive (UTF-8, ASCII)": (
        lambda: "é".encode("UTF-8") == "é".encode("utf-8") and "abc".encode("ASCII") == "abc".encode("ascii")  # noqa: UP012
    ),
    "json: ensure_ascii=None behaves as False": (
        lambda: json.dumps({"é": 1}, ensure_ascii=None) == json.dumps({"é": 1}, ensure_ascii=False)  # type: ignore[arg-type]
    ),
    "argparse: an option without default has the default None": (
        lambda: argparse.ArgumentParser().add_argument("--x").default is None
    ),
    "typing: cast returns its second argument whatever the first": lambda: typing.cast("int", "five") == "five",
    "dataclass: a control is blocking (warn_only False) unless declared otherwise": (
        lambda: QualityCheck("CTRL-X", "name", "high", "SELECT 1").warn_only is False
    ),
    "dataclass: a fresh cleaning report starts every counter at zero": (
        lambda: CleaningReport().currency_normalised == 0 and CleaningReport().status_normalised == 0
    ),
    "python: slicing nine dashes to eight gives eight dashes": lambda: ("-" * 9)[:8] == "-" * 8,
    "python: None and the empty string are both falsy in an `or` fallback": _falsy_values_fall_back_alike,
    "python: str.upper() makes 'info' and 'INFO' the same level name": lambda: "info".upper() == "INFO",
}


@pytest.mark.parametrize("premise", sorted(PREMISES))
def test_the_premise_of_an_equivalent_mutant_still_holds(premise: str) -> None:
    assert PREMISES[premise]() is True
