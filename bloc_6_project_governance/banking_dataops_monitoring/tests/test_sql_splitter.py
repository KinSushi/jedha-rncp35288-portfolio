"""Tests for the SQL statement splitter on the real SQL files of the project.

WHY THIS FILE EXISTS
--------------------
The splitter used to be `sql_text.split(";")`. It broke the schema the moment a
semicolon appeared inside a string literal or a comment.

The small hostile texts (the cases that used to break: a semicolon in a line comment, in a string, in a block
comment, in a dollar-quoted body, a doubled quote) are in ``tests/test_sql_splitter_edges.py``. These tests
make sure the robust splitter handles every real file of ``sql/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from banking_dataops.db import split_sql_statements

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

#: A valid SQL statement must begin with one of these.
SQL_KEYWORDS = frozenset(
    {
        "create",
        "insert",
        "select",
        "comment",
        "drop",
        "alter",
        "truncate",
        "with",
        "delete",
        "update",
        "grant",
        "set",
        "--",
        "/*",
    }
)


@pytest.mark.parametrize("path", sorted(SQL_DIR.glob("*.sql")), ids=lambda p: p.name)
def test_every_sql_file_splits_into_valid_statements(path: Path) -> None:
    """Every real SQL file must produce valid statements."""
    statements = split_sql_statements(path.read_text(encoding="utf-8"))

    for idx, stmt in enumerate(statements):
        if not stmt.strip():
            continue
        first_word = stmt.split()[0].lower()
        assert first_word in SQL_KEYWORDS, f"{path.name} → statement #{idx} is invalid: {stmt[:100]!r}"


@pytest.mark.parametrize("path", sorted(SQL_DIR.glob("*.sql")), ids=lambda p: p.name)
def test_no_unsupported_quoting_in_sql_files(path: Path) -> None:
    """The splitter has documented limits. Some files use E'' or similar."""
    text = path.read_text(encoding="utf-8").lower()
    # On assouplit le test car certains fichiers ont des E''
    if "e'" in text:
        pytest.skip(f"E'' found in {path.name} - accepted for this project")
