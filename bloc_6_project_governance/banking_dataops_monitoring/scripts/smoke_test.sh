#!/usr/bin/env bash
# Host-side half of the end-to-end check (ADR-0005): are the containers up, then everything the
# database can answer (scripts/smoke_check.py, run inside `app`), then the dashboard probed from
# the host on its published port, which smoke_check.py cannot see from inside the container.
# run.ps1 (Task-Smoke) makes the same split: the checks live only in smoke_check.py, which both
# launchers call.
# Called by `make smoke` and by scripts/preflight.sh, which relies on the exit code: 0 means the
# project is demonstrable.

# No -e: a failed check sets FAILED and the run goes on, so one run lists everything that is wrong.
set -uo pipefail

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YEL=$'\033[0;33m'; OFF=$'\033[0m'

ok()   { echo "${GREEN}  PASS${OFF}  $1"; }
ko()   { echo "${RED}  FAIL${OFF}  $1"; FAILED=1; }
step() { echo; echo "${YEL}==> $1${OFF}"; }

FAILED=0
# Read from the environment only: the .env written by run.ps1 is not read by this script.
DASHBOARD_PORT="${DASHBOARD_PORT:-8501}"

# Without the app container nothing below can run, so this one aborts. A missing postgres is
# recorded and the run continues.
step "0. Containers"
if docker compose ps --status running --services 2>/dev/null | grep -q '^app$'; then
  ok "app container is running"
else
  ko "app container is not running - start with: docker compose up -d"
  echo; echo "${RED}Aborted.${OFF}"; exit 1
fi

if docker compose ps --status running --services 2>/dev/null | grep -q '^postgres$'; then
  ok "postgres container is running"
else
  ko "postgres container is not running"
fi

docker compose exec -T app python scripts/smoke_check.py || FAILED=1

step "7. Dashboard reachable from the host"
if curl -fsS --max-time 10 "http://localhost:${DASHBOARD_PORT}/_stcore/health" >/dev/null 2>&1; then
  ok "dashboard answers on port ${DASHBOARD_PORT}"
else
  ko "dashboard does not answer - check: docker compose logs app"
fi

echo
if [[ $FAILED -eq 0 ]]; then
  echo "${GREEN}  ALL CHECKS PASSED${OFF}"
  exit 0
else
  echo "${RED}  NOT READY${OFF}"
  exit 1
fi
