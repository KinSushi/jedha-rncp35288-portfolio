"""Star-schema views (``banking_dataops.powerbi_views``): creation, the check that all eight exist, the refusal.

The SQL file is replaced by a recorder and the catalogue query is answered by the recording fake
(``tests/fakes.py``), so the tests read which file was applied and which view names were asked for.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeDatabase

from banking_dataops import audit, powerbi_views
from banking_dataops.config import Settings
from banking_dataops.powerbi_views import EXPECTED_VIEWS, create_views, list_views

pytestmark = pytest.mark.usefixtures("no_audit_database")

LIST_VIEWS_SQL = (
    "SELECT table_name FROM information_schema.views WHERE table_schema = 'public' "
    "AND table_name LIKE 'vw_%' ORDER BY table_name"
)


@pytest.fixture
def applied(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Settings | None]]:
    steps: list[tuple[Path, Settings | None]] = []
    monkeypatch.setattr(powerbi_views, "execute_sql_file", lambda path, settings=None: steps.append((path, settings)))
    return steps


def _catalogue(database: FakeDatabase, views: list[str]) -> None:
    database.answer("information_schema.views", rows=[(v,) for v in views], columns=["table_name"])


def test_the_eight_expected_views_are_the_ones_the_star_schema_file_creates() -> None:
    sql = (powerbi_views.STAR_SCHEMA_SQL).read_text(encoding="utf-8")

    assert len(set(EXPECTED_VIEWS)) == len(EXPECTED_VIEWS)
    for view in EXPECTED_VIEWS:
        assert f"VIEW {view}" in sql, f"{view} is expected but the SQL file does not create it"


def test_list_views_asks_the_public_schema_for_vw_views_and_returns_plain_names(
    fake_database: FakeDatabase, settings: Settings
) -> None:
    _catalogue(fake_database, ["vw_dim_account", "vw_dim_date"])

    names = list_views(settings)

    assert names == ["vw_dim_account", "vw_dim_date"]
    assert [" ".join(sql.split()) for sql in fake_database.sql_sent()] == [LIST_VIEWS_SQL]
    assert fake_database.dsns == [settings.dsn]


def test_list_views_of_an_empty_catalogue_is_an_empty_list(fake_database: FakeDatabase) -> None:
    _catalogue(fake_database, [])

    assert list_views() == []


def test_create_views_applies_the_star_schema_file_then_confirms_every_view(
    applied: list[tuple[Path, Settings | None]],
    fake_database: FakeDatabase,
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    _catalogue(fake_database, list(EXPECTED_VIEWS) + ["vw_something_else"])

    create_views(settings)

    assert applied == [(powerbi_views.ROOT / "sql" / "06_powerbi_star_schema.sql", settings)]
    events = [event for event, _ in audit_log]
    assert [(e.category, e.action, e.row_count) for e in events] == [
        ("pipeline", "run_started", None),
        ("integrity", "invariant_checked", None),
        ("data", "star_schema_views_created", 8),
        ("pipeline", "run_succeeded", None),
    ]
    invariant, created = events[1], events[2]
    assert invariant.details["invariant"] == "every declared star-schema view exists after creation"
    assert (invariant.details["expected"], invariant.details["missing"]) == (8, [])
    assert created.reason == "the star schema the Gold build and the Power BI model both read"
    assert created.details["views"] == list(EXPECTED_VIEWS)


def test_create_views_refuses_a_database_that_lacks_one_declared_view(
    applied: list[tuple[Path, Settings | None]],
    fake_database: FakeDatabase,
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    """FORBIDDEN PATH: a Power BI report that looks built and shows an empty panel."""
    _catalogue(fake_database, [v for v in EXPECTED_VIEWS if v != "vw_publication_verdict"])

    with pytest.raises(AssertionError, match="every declared star-schema view exists after creation"):
        create_views(settings)

    violated = next(event for event, _ in audit_log if event.action == "invariant_violated")
    assert violated.details["missing"] == ["vw_publication_verdict"]
    actions = [event.action for event, _ in audit_log]
    assert "star_schema_views_created" not in actions
    assert actions[-1] == "run_failed"


def test_create_views_without_settings_reads_them_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    applied: list[tuple[Path, Settings | None]],
    fake_database: FakeDatabase,
    settings: Settings,
) -> None:
    monkeypatch.setattr(powerbi_views, "load_settings", lambda: settings)
    _catalogue(fake_database, list(EXPECTED_VIEWS))

    create_views()

    assert applied[0][1] is settings
    assert fake_database.dsns == [settings.dsn]


def test_main_prints_ok_or_missing_for_each_expected_view(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    created: list[str] = []
    monkeypatch.setattr(powerbi_views, "create_views", lambda: created.append("created"))
    monkeypatch.setattr(powerbi_views, "list_views", lambda: [v for v in EXPECTED_VIEWS if v != "vw_dim_date"])

    powerbi_views.main()

    lines = capsys.readouterr().out.splitlines()
    assert created == ["created"]
    assert lines[0] == " MISSING  vw_dim_date"
    assert lines[1:] == [f"      ok  {view}" for view in EXPECTED_VIEWS[1:]]


def test_the_run_is_named_and_carries_the_settings_it_was_given(
    applied: list[tuple[Path, Settings | None]],
    fake_database: FakeDatabase,
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    _catalogue(fake_database, list(EXPECTED_VIEWS))

    create_views(settings)

    assert audit_log[0][0].reason == "step powerbi_views began"
    assert all(given is settings for event, given in audit_log if event.category != "integrity")


def test_a_missing_view_is_refused_with_the_rule_and_the_reason(
    applied: list[tuple[Path, Settings | None]], fake_database: FakeDatabase, settings: Settings
) -> None:
    _catalogue(fake_database, [])

    with pytest.raises(AssertionError) as refused:
        create_views(settings)

    assert str(refused.value) == (
        "Invariant violated: every declared star-schema view exists after creation. a missing view surfaces as an "
        "empty Power BI panel rather than an error; the report would look built and show nothing"
    )
