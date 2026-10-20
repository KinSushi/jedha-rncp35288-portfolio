"""The path aliases of the brief (``src/ingestion``, ``src/cleaning``, ``src/transformations``, ``src/ml``).

They exist so that the paths named by the brief resolve. They must re-export the real objects, never a copy,
and they must carry no logic: a second copy of a function would drift from the first within a week.
"""

from __future__ import annotations

import ast
import importlib
import tomllib
from pathlib import Path

import pytest

import banking_dataops

ROOT = Path(__file__).resolve().parents[1]

#: alias module -> (real module, names it re-exports)
ALIASES = {
    "ingestion.ingest": ("banking_dataops.generate_synthetic_data", ["SyntheticConfig", "generate", "main"]),
    "cleaning.clean": (
        "banking_dataops.pipeline.cleaning",
        ["CleaningReport", "clean_bronze_to_silver", "clean_transactions", "main"],
    ),
    "transformations.build_gold": ("banking_dataops.pipeline.gold", ["build", "main"]),
    "ml.predict": ("banking_dataops.ml.predict", ["load_model", "main", "predict"]),
    "ml.train": ("banking_dataops.ml.train", ["TrainingReport", "main", "train"]),
}


def test_an_alias_re_exports_the_very_objects_of_the_real_module_defines_nothing_and_refuses_any_other_name() -> None:
    for alias, (target, names) in sorted(ALIASES.items()):
        alias_module = importlib.import_module(alias)
        real_module = importlib.import_module(target)

        for name in names:
            assert getattr(alias_module, name) is getattr(real_module, name), f"{alias}.{name} is a copy"
        assert set(alias_module.__all__) >= set(names)

        source = (ROOT / "src" / (alias.replace(".", "/") + ".py")).read_text(encoding="utf-8")
        definitions = [n.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef | ast.ClassDef)]
        assert definitions == [], f"{alias} defines its own code"

        # FORBIDDEN PATH: the alias quietly growing a surface of its own.
        with pytest.raises(AttributeError):
            _ = alias_module.something_that_is_not_exported
        assert not hasattr(alias_module, "something_that_is_not_exported")

    # The ingestion alias also exposes the Silver load.
    from banking_dataops.ingest import ingest_all

    ingestion = importlib.import_module("ingestion.ingest")
    assert ingestion.ingest_all is ingest_all
    assert sorted(ingestion.__all__) == ["SyntheticConfig", "generate", "ingest_all", "main"]

    # The brief names these five paths and no other: a sixth does not resolve.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ingestion.not_in_the_brief")

    # The package version is the one declared in pyproject.toml.
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert banking_dataops.__version__ == declared == "0.1.0"
