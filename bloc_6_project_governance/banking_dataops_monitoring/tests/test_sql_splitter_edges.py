"""The SQL statement splitter (``banking_dataops.db.split_sql_statements``) on small hostile texts.

``tests/test_sql_splitter.py`` splits the real SQL files of the project. The table here holds the small cases,
accepted and refused rows together: the text ends right after a quote or a dollar sign, a tag is almost closed,
a positional parameter looks like the start of a tag, a string hides a comment marker, and a value that is not
text at all. Each row states the statements that a reader of the SQL would count, or the exception that refuses
the input.

The splitter used to be ``sql_text.split(";")``; it broke the schema the moment a semicolon appeared inside a
string literal or a comment, and the first rows below are the exact texts that broke it.

Every call runs under a watchdog. The splitter walks the text with an index; a wrong step would not fail an
assertion, it would loop for ever, and the mutation tool would call that a time-out and not a detection.
"""

from __future__ import annotations

from typing import Any

import pytest
from helpers import bounded

from banking_dataops.db import split_sql_statements


def split(text: Any) -> list[str]:
    return bounded(split_sql_statements, text)  # type: ignore[no-any-return]


AUDIT_TABLE = """
    -- The structured log answers what happened; this table answers it months later.
    CREATE TABLE audit_events (event_id BIGSERIAL PRIMARY KEY);
    """
COMMENT_ON_TABLE = """
    COMMENT ON TABLE gold.predictions IS
        'Prioritises investigation; never blocks publication (ADR-0002).';
    """
BLOCK_COMMENT = """
    /*
    This is a block comment with a ; inside it.
    It should not split the next statement.
    */
    CREATE TABLE t (id INT);
    """
FUNCTION_BODY = """
    CREATE FUNCTION test() RETURNS void AS $$
    BEGIN
        INSERT INTO audit_events VALUES ('x;y');
    END;
    $$ LANGUAGE plpgsql;
    """
TAGGED_BODY = """
    CREATE FUNCTION test() RETURNS void AS $func$
    BEGIN ... END;
    $func$ LANGUAGE plpgsql;
    """
MULTILINE_TABLE = "CREATE TABLE t (\n    id int, -- the key; unique\n    note text DEFAULT 'a;b'\n);"

#: (text given, statements expected) or (value given, exception that refuses it).
CASES: list[tuple[Any, Any]] = [
    # ---- ordinary text: cut at each semicolon, trimmed, empty pieces dropped, a last piece kept
    ("SELECT 1;SELECT 2;", ["SELECT 1", "SELECT 2"]),
    ("SELECT 1", ["SELECT 1"]),
    ("SELECT 1;\nSELECT 2;\n", ["SELECT 1", "SELECT 2"]),
    (";;;", []),
    ("", []),
    ("  \n\t ", []),
    ("  SELECT 1  ;  ", ["SELECT 1"]),
    ("SELECT 1; SELECT 2", ["SELECT 1", "SELECT 2"]),
    ("CREATE TABLE a (id INT); CREATE TABLE b (id INT);", ["CREATE TABLE a (id INT)", "CREATE TABLE b (id INT)"]),
    ("CREATE TABLE a (x int);\nINSERT INTO a VALUES (1)", ["CREATE TABLE a (x int)", "INSERT INTO a VALUES (1)"]),
    # ---- the text is returned as written, trimmed and nothing more
    (MULTILINE_TABLE, [MULTILINE_TABLE.removesuffix(";")]),
    # ---- strings: a semicolon, a comment marker or a dollar quote inside a literal is text
    ("SELECT ';' AS semi;", ["SELECT ';' AS semi"]),
    ("SELECT 'a;b';SELECT 'c;d'", ["SELECT 'a;b'", "SELECT 'c;d'"]),
    ("SELECT 'it''s; fine';SELECT 2", ["SELECT 'it''s; fine'", "SELECT 2"]),
    (
        "INSERT INTO test VALUES ('Jean''s data; with semicolon');",
        ["INSERT INTO test VALUES ('Jean''s data; with semicolon')"],
    ),
    ("SELECT '''';SELECT 2", ["SELECT ''''", "SELECT 2"]),
    ("SELECT '';SELECT 2", ["SELECT ''", "SELECT 2"]),
    ("SELECT '--not a comment; really';SELECT 2", ["SELECT '--not a comment; really'", "SELECT 2"]),
    ("SELECT '/* not a comment; */';SELECT 2", ["SELECT '/* not a comment; */'", "SELECT 2"]),
    ("SELECT '$$ not a quote; $$';SELECT 2", ["SELECT '$$ not a quote; $$'", "SELECT 2"]),
    ("SELECT 'a'||'b';SELECT 2", ["SELECT 'a'||'b'", "SELECT 2"]),
    (COMMENT_ON_TABLE, [COMMENT_ON_TABLE.strip().removesuffix(";")]),
    # FORBIDDEN PATH: cutting inside a string whose end is missing and running the pieces as statements.
    ("SELECT 'oops; DROP TABLE accounts;", ["SELECT 'oops; DROP TABLE accounts;"]),
    ("SELECT 'a'", ["SELECT 'a'"]),
    ("SELECT '", ["SELECT '"]),
    ("SELECT ''", ["SELECT ''"]),
    # ---- comments: a comment stays inside its statement and hides its semicolons
    (AUDIT_TABLE, [AUDIT_TABLE.strip().removesuffix(";")]),
    (BLOCK_COMMENT, [BLOCK_COMMENT.strip().removesuffix(";")]),
    ("-- it's a comment; with a quote\nSELECT 1;", ["-- it's a comment; with a quote\nSELECT 1"]),
    ("SELECT 1 -- c; d\n;SELECT 2", ["SELECT 1 -- c; d", "SELECT 2"]),
    ("/* block; with ' quote */ SELECT 1; SELECT 2;", ["/* block; with ' quote */ SELECT 1", "SELECT 2"]),
    ("/* a */ /* b; */ SELECT 1;", ["/* a */ /* b; */ SELECT 1"]),
    ("SELECT 1 /* ; */ ;SELECT 2", ["SELECT 1 /* ; */", "SELECT 2"]),
    ("SELECT 1; -- trailing; comment", ["SELECT 1", "-- trailing; comment"]),
    ("SELECT 1;\n-- a\n-- b\nSELECT 2;", ["SELECT 1", "-- a\n-- b\nSELECT 2"]),
    ("SELECT 5 - 3; SELECT 4 / 2;", ["SELECT 5 - 3", "SELECT 4 / 2"]),
    ("SELECT 1 /* x */", ["SELECT 1 /* x */"]),
    ("SELECT 1; /* never closed; SELECT 2;", ["SELECT 1", "/* never closed; SELECT 2;"]),
    ("/* a */", ["/* a */"]),
    ("SELECT 1;/**/", ["SELECT 1", "/**/"]),
    # ---- dollar quotes: a body is one piece whatever it contains, and only its own tag closes it
    (FUNCTION_BODY, [FUNCTION_BODY.strip().removesuffix(";")]),
    (TAGGED_BODY, [TAGGED_BODY.strip().removesuffix(";")]),
    (
        "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql; SELECT 2;",
        ["CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql", "SELECT 2"],
    ),
    ("DO $body$ BEGIN PERFORM 1; END $body$; SELECT 2", ["DO $body$ BEGIN PERFORM 1; END $body$", "SELECT 2"]),
    ("DO $_t1$ x; y $_t1$; SELECT 2", ["DO $_t1$ x; y $_t1$", "SELECT 2"]),
    ("DO $a1b2$ x; y $a1b2$; SELECT 2", ["DO $a1b2$ x; y $a1b2$", "SELECT 2"]),
    ("DO $a$ x; $ab$ y; $a$; SELECT 2", ["DO $a$ x; $ab$ y; $a$", "SELECT 2"]),
    ("DO $ab$ x; $a$ y; $ab$; SELECT 2", ["DO $ab$ x; $a$ y; $ab$", "SELECT 2"]),
    ("SELECT $$it's -- ; /* $$; SELECT 2", ["SELECT $$it's -- ; /* $$", "SELECT 2"]),
    ("SELECT $$$$; SELECT 2", ["SELECT $$$$", "SELECT 2"]),
    ("SELECT $x$$x$; SELECT 2", ["SELECT $x$$x$", "SELECT 2"]),
    ("SELECT $$ a $ b $$; SELECT 2", ["SELECT $$ a $ b $$", "SELECT 2"]),
    ("SELECT $t$ $$ ; $$ $t$; SELECT 2", ["SELECT $t$ $$ ; $$ $t$", "SELECT 2"]),
    ("SELECT $$ x $$ || $$ y; $$; SELECT 2", ["SELECT $$ x $$ || $$ y; $$", "SELECT 2"]),
    ("SELECT $$ a; $$", ["SELECT $$ a; $$"]),
    ("SELECT $t$ a; $t$", ["SELECT $t$ a; $t$"]),
    ("SELECT $t$ a; $t$;", ["SELECT $t$ a; $t$"]),
    ("DO $t$ a; $u$ b; SELECT 2;", ["DO $t$ a; $u$ b; SELECT 2;"]),
    # ---- FORBIDDEN PATH: a dollar sign that does not start a tag opens nothing (a positional parameter must not
    # swallow every statement that follows it), and a tag does not start with a digit
    ("SELECT $1; SELECT $2", ["SELECT $1", "SELECT $2"]),
    ("SELECT $1, $2; SELECT 3", ["SELECT $1, $2", "SELECT 3"]),
    ("SELECT 1 $; SELECT 2", ["SELECT 1 $", "SELECT 2"]),
    ("SELECT 1 $ ; SELECT 2", ["SELECT 1 $", "SELECT 2"]),
    ("SELECT 1 $", ["SELECT 1 $"]),
    ("$", ["$"]),
    ("SELECT '$'; SELECT 2", ["SELECT '$'", "SELECT 2"]),
    ("SELECT $-1; SELECT 2", ["SELECT $-1", "SELECT 2"]),
    ("SELECT $abc", ["SELECT $abc"]),
    ("SELECT $a", ["SELECT $a"]),
    ("SELECT $_", ["SELECT $_"]),
    ("SELECT $abc def; SELECT 2", ["SELECT $abc def", "SELECT 2"]),
    ("SELECT $abc; SELECT 2", ["SELECT $abc", "SELECT 2"]),
    ("SELECT $a$ x; $a$", ["SELECT $a$ x; $a$"]),
    ("SELECT $1abc$ x; SELECT 2", ["SELECT $1abc$ x", "SELECT 2"]),
    # ---- FORBIDDEN PATH: something that is not text is refused, instead of "no statement to run"
    (None, TypeError),
    (42, TypeError),
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_the_splitter_cuts_at_a_free_semicolon_only_and_refuses_what_is_not_text(text: Any, expected: Any) -> None:
    if isinstance(expected, type):
        with pytest.raises(expected):
            split(text)
    else:
        assert split(text) == expected
