#!/usr/bin/env bash
# Pre-flight check for the morning of the presentation (ADR-0005: stack started 75 minutes before,
# cold start under two minutes). It checks that Docker is up and the image already exists, restarts
# the stack from an empty volume and times the start, then runs scripts/smoke_test.sh.
# Called by `make preflight` and by hand at 5h30.
# It wipes the database: the cold start below is `docker compose down -v`.

set -uo pipefail

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YEL=$'\033[0;33m'; OFF=$'\033[0m'

echo "${YEL}=== PRE-FLIGHT  $(date '+%Y-%m-%d %H:%M:%S %Z') ===${OFF}"; echo

echo "1. Docker daemon"
if docker info >/dev/null 2>&1; then echo "${GREEN}   OK${OFF}"; else echo "${RED}   Docker is not running${OFF}"; exit 1; fi

# The image must exist already. Building on the day is refused: the dependencies are lower bounds
# only (requirements.txt), so a rebuild could pull a newer release. Build days ahead.
echo "2. Image already built"
if docker compose images app 2>/dev/null | grep -q app; then
  echo "${GREEN}   OK${OFF}"
else
  echo "${RED}   Image missing. Build it now, NOT on presentation day:${OFF}"
  echo "   docker compose build"
  exit 1
fi

# Worst case on purpose: an empty volume, so docker/entrypoint.sh runs the whole boot sequence
# (load, pipeline, reconciliation) before the dashboard is served. The 120 s budget is the cold
# start limit of ADR-0005.
echo "3. Cold start from an empty volume"
# Deliberately no --build, for the reason given at check 2.
docker compose down -v >/dev/null 2>&1
START=$(date +%s)
docker compose up -d >/dev/null 2>&1

# Polls for up to 180 s, longer than the budget, so that a slow start is measured and reported
# below instead of being cut off. Port 8501 is fixed here whereas smoke_test.sh honours
# DASHBOARD_PORT: with another host port this loop runs the full 180 s.
echo -n "   waiting for the dashboard "
for i in $(seq 1 180); do
  if curl -fsS --max-time 2 http://localhost:8501/_stcore/health >/dev/null 2>&1; then break; fi
  echo -n "."; sleep 1
done
echo

# Over budget is reported but does not stop the script: the smoke test still runs. The number of
# transactions is the --transactions option hard-coded in docker/entrypoint.sh.
ELAPSED=$(( $(date +%s) - START ))
if [[ $ELAPSED -lt 120 ]]; then
  echo "${GREEN}   OK - ready in ${ELAPSED}s${OFF}"
else
  echo "${RED}   SLOW - ${ELAPSED}s, over the 120s budget${OFF}"
  echo "   Lower --transactions in docker/entrypoint.sh, or free machine resources"
fi

echo "4. Full smoke test"
if ./scripts/smoke_test.sh; then
  echo
  echo "${GREEN}=== READY. Open http://localhost:8501 and do not touch anything else. ===${OFF}"
else
  echo
  echo "${RED}=== NOT READY. Fix before going live. ===${OFF}"; exit 1
fi
