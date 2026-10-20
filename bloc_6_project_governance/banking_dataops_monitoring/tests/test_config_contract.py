"""Settings (``banking_dataops.config``): defaults, the environment, the ``.env`` file, the connection string.

``tests/test_config.py`` checks that the defaults look right and that ``requirements.txt`` matches
``pyproject.toml``. These tests pin every field, every way a value can arrive, and what is refused.
"""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from pydantic import ValidationError

from banking_dataops import config
from banking_dataops.config import Settings, load_settings

FIELDS = [
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
    "DATABASE_SSLMODE",
    "DATA_DIR",
    "OUTPUT_DIR",
]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No variable of the project and no ``.env`` file of the developer may leak into these tests."""
    for field in FIELDS:
        monkeypatch.delenv(f"BANKING_DATAOPS_{field}", raising=False)
        monkeypatch.delenv(field, raising=False)
    monkeypatch.chdir(tmp_path)


def test_the_defaults_the_connection_string_and_the_masked_printout_are_exact_and_a_bad_port_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings()
    assert settings.model_dump() == {
        "database_host": "localhost",
        "database_port": 5432,
        "database_name": "banking_dataops",
        "database_user": "dataops",
        "database_password": "dataops",
        "database_sslmode": "",
        "data_dir": Path("data"),
        "output_dir": Path("output"),
    }

    # The connection string lists host, port, database, user and password in that order.
    custom = Settings(database_host="db.test", database_port=6543, database_name="bank", database_user="alice")
    assert custom.dsn == "host=db.test port=6543 dbname=bank user=alice password=dataops"

    # The SSL mode is appended only when it is configured.
    plain = Settings(database_sslmode="").dsn
    secured = Settings(database_sslmode="require").dsn
    assert "sslmode" not in plain
    assert plain == "host=localhost port=5432 dbname=banking_dataops user=dataops password=dataops"
    assert secured == plain + " sslmode=require"

    # Running the module prints the configuration with the password masked.
    monkeypatch.setenv("BANKING_DATAOPS_DATABASE_PASSWORD", "s3cret")
    monkeypatch.setenv("BANKING_DATAOPS_DATABASE_HOST", "db.test")
    runpy.run_path(str(Path(config.__file__)), run_name="__main__")
    out = capsys.readouterr().out
    assert "s3cret" not in out
    assert out.splitlines() == [
        "=" * 80,
        "BANKING DATAOPS MONITORING - CONFIGURATION",
        "=" * 80,
        "Database Host:     db.test",
        "Database Port:     5432",
        "Database Name:     banking_dataops",
        "Database User:     dataops",
        "Data Directory:    data",
        "Output Directory:  output",
        "DSN:               host=db.test port=5432 dbname=banking_dataops user=dataops password=***",
        "=" * 80,
    ]

    # FORBIDDEN PATH: a port that is not a number, given as an argument, is refused naming the field.
    with pytest.raises(ValidationError, match="database_port"):
        Settings(database_port="five-four-three-two")  # type: ignore[arg-type]


def test_a_value_comes_from_the_environment_then_the_env_file_and_a_foreign_or_malformed_one_is_not_taken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A value in the environment overrides the default, and a port is read as a number.
    monkeypatch.setenv("BANKING_DATAOPS_DATABASE_HOST", "postgres")
    monkeypatch.setenv("BANKING_DATAOPS_DATABASE_PORT", "6543")
    monkeypatch.setenv("BANKING_DATAOPS_OUTPUT_DIR", "reports")
    settings = Settings()
    assert (settings.database_host, settings.database_port, settings.output_dir) == ("postgres", 6543, Path("reports"))
    assert isinstance(settings.database_port, int)

    # An unknown variable with the project prefix is ignored.
    monkeypatch.setenv("BANKING_DATAOPS_NOT_A_SETTING", "x")
    assert not hasattr(Settings(), "not_a_setting")

    # The ``.env`` file of the working directory is read and the process environment wins.
    (tmp_path / ".env").write_text(
        "BANKING_DATAOPS_DATABASE_NAME=from_file\nBANKING_DATAOPS_DATABASE_USER=file_user\n", encoding="utf-8"
    )
    monkeypatch.setenv("BANKING_DATAOPS_DATABASE_USER", "env_user")
    from_file = Settings()
    assert (from_file.database_name, from_file.database_user) == ("from_file", "env_user")

    # ``load_settings`` builds a fresh object each time.
    (tmp_path / ".env").unlink()
    first = load_settings()
    monkeypatch.setenv("BANKING_DATAOPS_DATABASE_NAME", "changed")
    second = load_settings()
    assert isinstance(first, Settings)
    assert (first.database_name, second.database_name) == ("banking_dataops", "changed")

    # FORBIDDEN PATH: a variable of another application changing which database this one reaches.
    for field in ("HOST", "PORT", "NAME", "USER", "PASSWORD"):
        monkeypatch.delenv(f"BANKING_DATAOPS_DATABASE_{field}", raising=False)
    monkeypatch.setenv("DATABASE_HOST", "somebody-elses-host")
    monkeypatch.setenv("DATABASE_PASSWORD", "somebody-elses-password")
    unprefixed = Settings()
    assert (unprefixed.database_host, unprefixed.database_password) == ("localhost", "dataops")

    # FORBIDDEN PATH: a typo in the environment turning into a connection to port 0 or to the default.
    monkeypatch.setenv("BANKING_DATAOPS_DATABASE_PORT", "five-four-three-two")
    with pytest.raises(ValidationError, match="database_port"):
        Settings()
