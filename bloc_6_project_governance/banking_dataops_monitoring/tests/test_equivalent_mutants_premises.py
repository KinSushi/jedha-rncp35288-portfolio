"""The premises on which the mutants listed in ``MUTANTS_EQUIVALENTS.md`` are called equivalent.

A mutant is equivalent when no test can tell it from the original because the two behave identically. For
most of them the reason is a fact about a library or about Python (a default value, a codec alias, the
truthiness of ``None``). Each such fact is checked here, so that the claim in the document does not rest on
memory: if a library upgrade ever changes one of them, this test fails and the mutant has to be looked at again.

A premise is a fact the proof uses, not the proof itself: a check that passes on a few sample values does not show
that no input can tell the mutant from the original. The proof is the reading of the code written in
``MUTANTS_EQUIVALENTS.md``, and the facts are checked in the environment of the measurement listed there.
"""

from __future__ import annotations

import argparse
import io
import json
import typing
from collections.abc import Callable

import pandas as pd
import pytest
from sklearn.compose import ColumnTransformer
from sklearn.metrics import confusion_matrix

from banking_dataops.pipeline.cleaning import CleaningReport
from banking_dataops.quality_checks import QualityCheck


def _falsy_values_fall_back_alike() -> bool:
    absent: object = None
    empty: object = ""
    return (absent or "-") == (empty or "-")


def _duplicated_keeps_the_first_by_default() -> bool:
    frame = pd.DataFrame({"k": [1, 2, 1, 1], "v": [10, 20, 30, 40]})
    default = frame.duplicated(subset=["k"]).tolist()
    return default == frame.duplicated(subset=["k"], keep="first").tolist() == [False, False, True, True]


def _csv_text(index: object) -> str:
    buffer = io.StringIO()
    pd.DataFrame({"a": [1, 2]}).to_csv(buffer, index=index)  # type: ignore[arg-type]
    return buffer.getvalue()


#: premise -> a check that returns True when the fact still holds. One row per family of equivalent mutants.
#:
#: Removed on 19/10/2026: "numpy: a positive factor does not move the argmax of the F1 curve". It held for the
#: random, continuous precisions and recalls it drew, which never tie, but not for a real curve, where two points
#: often have exactly the same F1. Ties are decided by the last bit of the computed values, and that bit depends on
#: the factor: on some inputs the original (factor 2) computes the tied values identically and returns the lowest
#: threshold, while the factor 3 rounds them apart and returns a higher one. The original does not do this on every
#: exact tie either (see the docstring of ``_choose_threshold``). The mutant it covered (``_choose_threshold`` #18)
#: was not equivalent; it is now killed by
#: ``test_choose_threshold_returns_the_lowest_threshold_of_a_tie_whose_f1_is_not_exact_in_binary``
#: (``tests/test_ml_training_contract.py``).
PREMISES: dict[str, Callable[[], bool]] = {
    "sklearn: ColumnTransformer drops the unlisted columns by default": (
        lambda: ColumnTransformer(transformers=[]).remainder == "drop"
    ),
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
    "pandas: duplicated marks every row but the first by default (keep='first')": (
        _duplicated_keeps_the_first_by_default
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
