#!/bin/bash
# Boot sequence of the `app` container (CMD of the Dockerfile, service of docker-compose.yml,
# ADR-0005): wait for PostgreSQL, generate the synthetic batch (ADR-0003), load it, run the
# pipeline (batch processing, ADR-0001), reconcile, then serve the API (ADR-0006) and the
# dashboard until the container is stopped.
# The Dockerfile starts it with `sh`, so the shebang above is not used there.
#
# Nothing here trains or scores the model: train it on demand (make ml-train, or
# run_pipeline.py --train) and score it with make ml-predict (ADR-0008). TRAIN_ON_BOOT in
# docker-compose.yml is not read by this script.
#
# Date anchor of the generated batch (ADR-0009): BANKING_DATAOPS_GENERATED_AT, which
# docker-compose.yml forwards from the host, or the option `--generated-at VALUE` of this
# script (an ISO date or `today`), which wins over the variable, as on run_pipeline.py.
# Neither given: the fixed default date.

# A failing command ends the script, and so the container, before the API and the dashboard are
# started: a failed pipeline leaves nothing served. With `restart: unless-stopped` in
# docker-compose.yml, Docker then runs the whole sequence again.
set -e

# `--generated-at` is the only option. Any other argument is refused (exit 2) rather than
# ignored, because a mistyped option would otherwise start a batch with the wrong date,
# unnoticed. The check comes first: nothing is generated or loaded before it passes.
while [ "$#" -gt 0 ]; do
    case "$1" in
        --generated-at)
            if [ -z "${2:-}" ]; then
                echo "[boot] --generated-at needs a value (an ISO date, or today)" >&2
                exit 2
            fi
            BANKING_DATAOPS_GENERATED_AT="$2"
            export BANKING_DATAOPS_GENERATED_AT
            shift 2
            ;;
        *)
            echo "[boot] unknown argument: $1 (only --generated-at is accepted)" >&2
            exit 2
            ;;
    esac
done

echo "[boot] Waiting for PostgreSQL..."

# The probe exits 0 only when the connection works (its previous form wrapped sys.exit(0) in a bare
# `except:`, which caught the exit and made every attempt fail, so the loop always ran its full 120 s).
# 60 attempts, 2 s apart. Giving up does not fail the script: it carries on, and the first step
# that needs the database (the ingest below) stops it. The probe hard-codes the connection
# parameters of docker-compose.yml instead of reading src/banking_dataops/config.py: change one,
# change the other.
for i in $(seq 1 60); do
    if python -c '
import psycopg
import sys
try:
    conn = psycopg.connect(host="postgres", port=5432, user="dataops", password="dataops", dbname="banking_dataops")
    conn.close()
except Exception:
    sys.exit(1)
print("[boot] PostgreSQL ready!")
' 2>/dev/null; then
        break
    fi
    sleep 2
done

# Same values as the defaults of run_pipeline.py (seed 42, 100 customers, 1500 transactions). No
# --generated-at is passed to the generator or to run_pipeline.py: both read
# BANKING_DATAOPS_GENERATED_AT, exported above when the option was given. Without any value the
# fixed default date applies, and every boot produces the same rows (ADR-0003, ADR-0009).
# run_pipeline.py below generates and loads this batch again; the load is TRUNCATE then INSERT,
# so the repetition costs time and nothing else.
echo "[boot] Generating synthetic data..."
python -m banking_dataops.generate_synthetic_data --output-dir data --customers 100 --transactions 1500 --seed 42

echo "[boot] Ingest + Schema + Controls + Reconciliation + Gold..."
python -m banking_dataops.ingest
# --skip-ml: no training. Scoring is not part of run_pipeline.py either, so gold.predictions is not
# filled by this sequence: that is make ml-predict.
python run_pipeline.py --skip-ml

# Reconciliation is not a step of run_pipeline.py, hence its own call.
echo "[boot] Running reconciliation..."
python -m banking_dataops.reconciliation

# Both listen on 0.0.0.0 so that the ports published by docker-compose.yml reach them from outside
# the container. They run in the background and `wait` keeps the script, hence the container,
# alive until both have exited.
echo "[boot] Starting services..."
uvicorn banking_dataops.api:app --host 0.0.0.0 --port 8000 &
streamlit run dashboard/streamlit_app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true &

wait
