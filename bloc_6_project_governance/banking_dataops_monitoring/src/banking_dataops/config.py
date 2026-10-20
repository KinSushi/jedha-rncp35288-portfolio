#!/usr/bin/env python3
"""Runtime configuration: database connection and working directories, read from the environment.

One place decides how the project reaches PostgreSQL and where it reads and writes files, so
that no module hard-codes a host or a path and the same code runs on a laptop, in Docker and
in a managed cloud database.

CHOIX
-----
Retained:

- ``pydantic-settings``: values come from environment variables or a ``.env`` file, are typed
  and validated (a port that is not an integer is refused), and have development defaults.
- A ``BANKING_DATAOPS_`` prefix on every variable, so the project's settings cannot collide
  with another library's (``DATABASE_URL``, for instance).
- ``dsn`` is a property computed on demand, not a stored field, in libpq ``key=value`` form.
  ``sslmode`` is appended only when set, so the local DSN is unchanged and a managed database
  (Neon, Supabase) is reached with ``BANKING_DATAOPS_DATABASE_SSLMODE=require``.
- ``extra="ignore"``: an unknown variable in ``.env`` is ignored rather than fatal.

Rejected:

- Hard-coded values per module: they drift between stages and put credentials in source.
- A URL-style DSN: the ``key=value`` form copes with special characters in a password.
  Source : inconnue (reason inherited from the original design, not re-measured here).
- A secrets manager: out of scope for a local teaching stack (ADR-0005); the defaults here
  are development values and the module says so.

OÙ
---
Role: configuration layer, imported by the modules that touch the database. Callers:
``banking_dataops.db`` (connection), ``banking_dataops.audit``, ``run_pipeline.py``
(validation at start-up) and every module that takes a ``settings`` argument. The variables
are set by ``docker-compose.yml`` (service ``app``), by ``.env.example`` for local use, and by
the cloud connection guide (docs/powerbi/connexion-cloud-neon-supabase.md). Depends on
``pydantic`` and ``pydantic-settings``.

COMMENT
-------
``Settings()`` reads the process environment, then ``.env`` in the working directory, then
falls back on the defaults. ``load_settings`` is the factory every caller uses, which gives
tests one function to replace. Running the module (``python -m banking_dataops.config``)
prints the effective configuration with the password masked.
Tests that prove it: tests/test_config.py (``test_settings_defaults``,
``test_requirements_txt_matches_pyproject``).

POURQUOI
--------
- Settings from the environment: containers receive their configuration that way
  (docker-compose.yml, ADR-0005).
- Development defaults (``dataops`` for user and password): the stack is local and synthetic;
  they must be overridden for anything else. Source : inconnue for the choice of the values.
- Passwords are never read from source code; ``.env`` is ignored by git (.gitignore).

JOURNALISATION
--------------
Nothing is logged: this module only builds an object. The ``dsn`` carries the password, so it
must not be logged; the ``__main__`` block masks it before printing, and that block is the
only place the module prints, because it is a command-line entry point.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed settings, filled from the environment, then ``.env``, then the defaults below.

    ``BaseSettings`` rather than ``BaseModel``: it does the environment parsing, which
    containers need because their configuration is injected as environment variables.
    """

    model_config = SettingsConfigDict(env_file=".env", env_prefix="BANKING_DATAOPS_", extra="ignore")

    database_host: str = Field(default="localhost", description="PostgreSQL server hostname or IP address")

    database_port: int = Field(default=5432, description="PostgreSQL server port (standard: 5432)")

    database_name: str = Field(default="banking_dataops", description="PostgreSQL database name")

    database_user: str = Field(default="dataops", description="PostgreSQL authentication username")

    database_password: str = Field(
        default="dataops", description="PostgreSQL authentication password (override in production)"
    )

    database_sslmode: str = Field(default="", description="psycopg sslmode (empty=local; 'require' for Neon/Supabase)")

    # Working directories: ``data_dir`` is read by ingest.py, ``output_dir`` by incident_report.py.
    # run_pipeline.py writes the Bronze files to the literal path "data", so the two agree only
    # while ``data_dir`` keeps its default.
    data_dir: Path = Field(default=Path("data"), description="Root directory for data layers (raw/silver/gold)")

    output_dir: Path = Field(default=Path("output"), description="Directory for generated reports and exports")

    @property
    def dsn(self) -> str:
        """Return the libpq ``key=value`` connection string. It contains the password: never log it."""
        return (
            f"host={self.database_host} "
            f"port={self.database_port} "
            f"dbname={self.database_name} "
            f"user={self.database_user} "
            f"password={self.database_password}"
            # Append sslmode ONLY when configured. Empty locally => the key is
            # omitted and the DSN is byte-for-byte the original one. Set to
            # "require" to reach Neon / Supabase and other managed TLS-only DBs.
            + (f" sslmode={self.database_sslmode}" if self.database_sslmode else "")
        )


def load_settings() -> Settings:
    """Return the settings. Callers use this rather than ``Settings()``, so there is one place to replace in a test."""
    return Settings()


if __name__ == "__main__":
    settings = load_settings()
    print("=" * 80)
    print("BANKING DATAOPS MONITORING - CONFIGURATION")
    print("=" * 80)
    print(f"Database Host:     {settings.database_host}")
    print(f"Database Port:     {settings.database_port}")
    print(f"Database Name:     {settings.database_name}")
    print(f"Database User:     {settings.database_user}")
    print(f"Data Directory:    {settings.data_dir}")
    print(f"Output Directory:  {settings.output_dir}")
    print(f"DSN:               {settings.dsn.replace(settings.database_password, '***')}")
    print("=" * 80)
