"""The container entrypoint passes the date anchor (ADR-0009) down to the generator and the pipeline.

The anchor reaches the containers two ways: the variable BANKING_DATAOPS_GENERATED_AT, which
docker-compose.yml forwards from the host, and the option ``--generated-at`` of docker/entrypoint.sh.
The tests run the real script with stand-ins for ``python``, ``uvicorn`` and ``streamlit`` that only
record the value of the variable each one is started with, so no database, Docker or network is used.
They are skipped where no ``sh`` is installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None, reason="no POSIX shell available")

# Every stand-in appends "<name>|<first argument>|<value of the variable>" to the log, then exits 0.
STAND_IN = """#!/bin/sh
printf '%s|%s|%s\\n' "$(basename "$0")" "$1" "${BANKING_DATAOPS_GENERATED_AT-unset}" >> "$ENTRYPOINT_LOG"
"""


def run_entrypoint(tmp_path: Path, *args: str, variable: str | None = None) -> tuple[int, list[list[str]]]:
    """Run docker/entrypoint.sh with stand-ins on the PATH; return its exit code and the recorded calls.

    Each run starts from an empty log, so several runs can follow one another in the same test.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("python", "uvicorn", "streamlit"):
        stand_in = bin_dir / name
        stand_in.write_bytes(STAND_IN.encode("utf-8"))
        stand_in.chmod(0o755)
    log = tmp_path / "calls.log"
    log.unlink(missing_ok=True)
    env = {**os.environ, "PATH": os.pathsep.join([str(bin_dir), os.environ["PATH"]]), "ENTRYPOINT_LOG": str(log)}
    env.pop("BANKING_DATAOPS_GENERATED_AT", None)
    if variable is not None:
        env["BANKING_DATAOPS_GENERATED_AT"] = variable
    assert SH is not None
    done = subprocess.run(
        [SH, "docker/entrypoint.sh", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    calls = [line.split("|") for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return done.returncode, calls


def test_the_option_beats_the_variable_and_the_variable_beats_the_default_and_a_bad_option_is_refused_before_any_step(
    tmp_path: Path,
) -> None:
    # The option reaches every step.
    exit_code, calls = run_entrypoint(tmp_path, "--generated-at", "today")
    assert exit_code == 0
    assert {value for _, _, value in calls} == {"today"}
    assert {name for name, _, _ in calls} == {"python", "uvicorn", "streamlit"}
    assert ["python", "run_pipeline.py", "today"] in calls

    # The variable forwarded by compose is used without the option, and the option wins over the variable.
    exit_code, calls = run_entrypoint(tmp_path, variable="2026-09-01")
    assert exit_code == 0
    assert {value for _, _, value in calls} == {"2026-09-01"}
    exit_code, calls = run_entrypoint(tmp_path, "--generated-at", "2026-10-11", variable="2026-09-01")
    assert exit_code == 0
    assert {value for _, _, value in calls} == {"2026-10-11"}

    # Without option or variable nothing is set: the fixed default of the code applies, and the entrypoint must not
    # invent a value.
    exit_code, calls = run_entrypoint(tmp_path)
    assert exit_code == 0
    assert {value for _, _, value in calls} == {"unset"}

    # FORBIDDEN PATHS: an option without its value, or a mistyped option, must not start a batch with the default
    # date, unnoticed. Both are refused before any step runs.
    exit_code, calls = run_entrypoint(tmp_path, "--generated-at")
    assert exit_code == 2
    assert calls == []
    exit_code, calls = run_entrypoint(tmp_path, "--generated_at", "today")
    assert exit_code == 2
    assert calls == []
