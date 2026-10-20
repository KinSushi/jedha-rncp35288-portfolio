"""Tests for the living-documentation checker (scripts/verifier_doc_vivante.py).

The direct test runs the checker on the real tree (``src/``, ``scripts/``, ``tools/``, ``dashboard/``,
``huggingface-space/`` and ``run_pipeline.py``): every module must carry its five sections and every citation must
resolve. The reverse tests build tiny trees and show that each kind of defect is refused.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("verifier_doc_vivante", REPO_ROOT / "scripts" / "verifier_doc_vivante.py")
checker = importlib.util.module_from_spec(SPEC)
sys.modules["verifier_doc_vivante"] = checker
SPEC.loader.exec_module(checker)

FULL = '''"""A module.

CHOIX
-----
Retained: a thing. Rejected: another thing.

OÙ
---
Role: nowhere in particular.

COMMENT
-------
It works. {tests}

POURQUOI
--------
{why}

JOURNALISATION
--------------
Nothing is logged.
"""
'''


def _tree(tmp_path: Path, docstring_module: str, *, tests: str = "", why: str = "source : inconnue") -> Path:
    """Build a minimal repository: one module, one ADR, one test file with one test."""
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs" / "adr" / "0001-first.md").write_text("# ADR-0001\n", encoding="utf-8")
    (tmp_path / "tests" / "test_sample.py").write_text("def test_real_one():\n    pass\n", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "notes.md").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "mod.py").write_text(
        docstring_module.format(tests=tests, why=why) + "\nVALUE = 1\n", encoding="utf-8"
    )
    return tmp_path


def _kinds(root: Path) -> list[str]:
    return sorted(f.kind for f in checker.check_tree(root))


def _exit_code(root: Path) -> int:
    """What the command line, the Makefile and the CI see: 0 for a clean tree, 1 for any finding."""
    return int(checker.main(["--root", str(root)]))


# ---------------------------------------------------------------------------
# Direct
# ---------------------------------------------------------------------------


def test_every_python_module_of_the_project_documents_itself() -> None:
    """The real tree conforms: five sections each, and every citation resolves."""
    assert checker.check_tree(REPO_ROOT) == []


def test_a_complete_module_with_resolving_citations_passes(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        FULL,
        tests="Tested by ``test_real_one``.",
        why="See ADR-0001 and src/pkg/notes.md:2-3.",
    )
    assert checker.check_tree(root) == []


def test_an_honest_unknown_source_is_accepted(tmp_path: Path) -> None:
    assert checker.check_tree(_tree(tmp_path, FULL, why="The value is a project choice, source : inconnue.")) == []


def test_a_package_file_without_code_is_exempt(tmp_path: Path) -> None:
    root = _tree(tmp_path, FULL)
    (root / "src" / "pkg" / "__init__.py").write_text('"""Only a docstring."""\n__all__ = []\n', encoding="utf-8")
    assert checker.check_tree(root) == []


# ---------------------------------------------------------------------------
# Reverse: each defect is refused
# ---------------------------------------------------------------------------


def test_a_missing_section_is_refused(tmp_path: Path) -> None:
    without_journal = FULL.replace("JOURNALISATION\n--------------\nNothing is logged.\n", "")
    root = _tree(tmp_path, without_journal)
    findings = checker.check_tree(root)
    assert [(f.kind, f.detail) for f in findings] == [("missing-section", "JOURNALISATION")]
    assert _exit_code(root) == 1


def test_an_empty_section_counts_as_missing(tmp_path: Path) -> None:
    empty_choice = FULL.replace("Retained: a thing. Rejected: another thing.\n", "")
    root = _tree(tmp_path, empty_choice)
    findings = checker.check_tree(root)
    assert [(f.kind, f.detail) for f in findings] == [("missing-section", "CHOIX")]
    assert _exit_code(root) == 1


def test_a_module_without_a_docstring_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path, FULL)
    (root / "src" / "pkg" / "bare.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert _kinds(root) == ["no-docstring"]
    assert _exit_code(root) == 1


def test_a_citation_to_a_missing_file_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path, FULL, why="See src/pkg/ghost.py:3.")
    assert _kinds(root) == ["missing-file"]
    assert _exit_code(root) == 1


def test_a_line_number_beyond_the_end_of_the_file_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path, FULL, why="See src/pkg/notes.md:99.")
    assert _kinds(root) == ["line-out-of-range"]
    assert _exit_code(root) == 1


def test_an_unknown_adr_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path, FULL, why="Decided in ADR-0042.")
    assert _kinds(root) == ["missing-adr"]
    assert _exit_code(root) == 1


def test_a_cited_test_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path, FULL, tests="Tested by ``test_imaginary``.")
    assert _kinds(root) == ["missing-test"]
    assert _exit_code(root) == 1


def test_an_unresolved_marker_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path, FULL, why="See @@src/pkg/notes.md::two@@.")
    assert _kinds(root) == ["unresolved-marker"]
    assert _exit_code(root) == 1


def test_the_command_line_exits_1_on_a_finding_and_0_on_a_clean_tree(tmp_path: Path) -> None:
    bad = _tree(tmp_path / "bad", FULL, why="See ADR-0042.")
    good = _tree(tmp_path / "good", FULL)
    assert checker.main(["--root", str(bad)]) == 1
    assert checker.main(["--root", str(good)]) == 0


@pytest.mark.parametrize("citation", ["models/suspicion_classifier.joblib", "data/synthetic_customers.csv"])
def test_files_generated_by_the_pipeline_are_not_demanded(tmp_path: Path, citation: str) -> None:
    """Counter-check: a generated artefact is absent from a fresh checkout and must not be required."""
    assert checker.check_tree(_tree(tmp_path, FULL, why=f"It writes {citation}.")) == []


def test_a_zone_may_be_a_single_file(tmp_path: Path) -> None:
    """``run_pipeline.py`` is a file at the root of the project, not a directory: it is checked too."""
    module = FULL.format(tests="", why="source : inconnue") + "\nVALUE = 1\n"
    (tmp_path / "solo.py").write_text(module, encoding="utf-8")
    assert checker.check_tree(tmp_path, ("solo.py",)) == []


def test_a_single_file_zone_without_a_docstring_is_refused(tmp_path: Path) -> None:
    (tmp_path / "solo.py").write_text("VALUE = 1\n", encoding="utf-8")
    findings = checker.check_tree(tmp_path, ("solo.py",))
    assert [(f.path, f.kind) for f in findings] == [("solo.py", "no-docstring")]


def test_a_title_underlined_with_two_dashes_does_not_open_a_section(tmp_path: Path) -> None:
    """The common gate reads a title as a section only with three or more dashes under it; so does this checker."""
    two_dashes = FULL.replace("OÙ\n---\n", "OÙ\n--\n")
    root = _tree(tmp_path, two_dashes)
    findings = checker.check_tree(root)
    assert [(f.kind, f.detail) for f in findings] == [("missing-section", "OU")]
    assert _exit_code(root) == 1
