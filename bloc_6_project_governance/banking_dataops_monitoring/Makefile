# Task runner for Linux and macOS. run.ps1 is the Windows counterpart, under different names.
# Targets that call `docker compose` act on the containers (ADR-0005). The others run Python or
# shell on the host and need `make install`; those that touch the database reach it at
# localhost:5432 unless BANKING_DATAOPS_DATABASE_* says otherwise (src/banking_dataops/config.py).

# No target produces a file. `dashboard` is also a directory of the repository: without .PHONY,
# make would consider that target up to date and do nothing.
.PHONY: help install test lint ci up down generate ingest quality reconcile incident dashboard reset clean \
	stack-up stack-down stack-reset demo-inject demo-reset demo-status \
	smoke preflight demo-inject-app demo-reset-app api-open api-verdict lock \
	powerbi-views powerbi-check audit-trail audit-runs reverse-test \
	pipeline pipeline-train pipeline-verify ml-train ml-predict clean-bronze eda integration-test

# First target of the file, so a bare `make` prints this list.
help:
	@echo ""
	@echo "  banking-dataops-monitoring"
	@echo ""
	@echo "  START HERE"
	@echo "    make stack-up          build and start database, pipeline, dashboard and API"
	@echo "    make pipeline          run bronze -> silver -> gold in one command (the model needs --train)"
	@echo "    make pipeline-train    same, retraining the model"
	@echo "    make pipeline-verify   run with --verify (one run, no extra check yet)"
	@echo "    make smoke             end-to-end check - must print DEMO READY"
	@echo "    make preflight         presentation-day cold start, timed"
	@echo ""
	@echo "  DEMO SCENARIO"
	@echo "    make demo-inject-app   inject the degraded batch (inside the container)"
	@echo "    make demo-reset-app    remove it and re-run the controls"
	@echo "    make demo-status       how many demo rows are present"
	@echo ""
	@echo "  STACK"
	@echo "    make stack-down        stop, keep the data"
	@echo "    make stack-reset       wipe the volume and restart"
	@echo "    make api-open          show the API endpoints"
	@echo "    make powerbi-views     create the star-schema views for Power BI"
	@echo ""
	@echo "  PIPELINE STEP BY STEP"
	@echo "    make up generate ingest quality reconcile incident dashboard"
	@echo "    WARNING: make ingest empties and reloads the Silver tables (not the audit journal)"
	@echo ""
	@echo "  MACHINE LEARNING"
	@echo "    make ml-train          train, compare baseline vs forest, persist .joblib"
	@echo "    make ml-predict        score the batch into gold.predictions"
	@echo ""
	@echo "  CODE QUALITY"
	@echo "    make install lint test ci"
	@echo "    make lock              regenerate the pinned dependency file"
	@echo "    make reverse-test      run the forbidden-path suite only"
	@echo "    make integration-test  run the cross-module suite only"
	@echo "    make eda               open the exploratory notebook"
	@echo ""
	@echo "  AUDIT"
	@echo "    make audit-trail       last 30 journal events, with their business reason"
	@echo "    make audit-runs        one line per run: duration, failures, violations"
	@echo ""
	@echo "  Documentation: README.md   Decisions: docs/adr/"
	@echo ""

# Editable install with the dev extra (pytest, ruff, httpx: see pyproject.toml), on the host.
install:
	python -m pip install --upgrade pip
	pip install -e ".[dev]"

# On the host. testpaths and pythonpath come from [tool.pytest.ini_options] in pyproject.toml.
test:
	pytest

# Rules and line length come from [tool.ruff] in pyproject.toml.
lint:
	ruff check .

# The two commands of .github/workflows/ci.yml, in the same order.
ci: lint test

# Starts both services, postgres and app, in the background. The entrypoint of app then loads the
# data and runs the pipeline (docker/entrypoint.sh): this is not a PostgreSQL-only start.
up:
	docker compose up -d

# Removes the containers and keeps the postgres_data volume. `down -v` (see stack-reset) removes it too.
down:
	docker compose down

# Writes the synthetic CSV files to data/ on the host. The counts are the generator's defaults, like
# seed 42 and the fixed date anchor (ADR-0003, ADR-0009).
generate:
	python -m banking_dataops.generate_synthetic_data --output-dir data --customers 100 --transactions 1500

# Truncates customers, accounts, transactions and the quality, reconciliation and incident result
# tables, then reloads them from data/synthetic_*.csv. TRUNCATE then INSERT, never an upsert, so a
# rerun gives the same tables. Demo rows are removed too. audit_events is not truncated.
ingest:
	python -m banking_dataops.ingest

# Runs the seven SQL controls (sql/02_data_quality_checks.sql) and stores each result in
# quality_check_results. The publication verdict is derived from them by rules, never by the
# model (ADR-0002).
quality:
	python -m banking_dataops.quality_checks

# Writes coherence summaries of transactions (by source system, date, channel, status and suspicious
# flag) to reconciliation_results. Both sides of each delta come from the same table: this checks
# coherence, it is not a reconciliation against an independent system (CHANGELOG 2.4.0).
reconcile:
	python -m banking_dataops.reconciliation

# Re-runs the controls, then writes output/incident_report.md if one of them is FAIL or WARN.
# With nothing to report, no file is written.
incident:
	python -m banking_dataops.incident_report

# Runs the dashboard on the host. The app container serves its own copy.
dashboard:
	streamlit run dashboard/streamlit_app.py

# Host-side reset: drops the volume, restarts the stack, then regenerates, reloads and re-checks
# from the host. `up -d` also starts app, whose entrypoint loads the same data, and the host
# commands below do not wait for it.
reset:
	docker compose down -v
	docker compose up -d
	python -m banking_dataops.generate_synthetic_data --output-dir data --customers 100 --transactions 1500
	python -m banking_dataops.ingest
	python -m banking_dataops.quality_checks
	python -m banking_dataops.reconciliation

# Deletes the generated CSV files and Markdown reports (data/*.csv, output/*.md). The database and
# models/ are untouched.
clean:
	rm -f data/*.csv
	rm -f output/*.md

# Rebuilds the app image, then starts both services in the foreground (no -d), streaming their logs.
stack-up:
	docker compose up --build

# Same command as `down`: containers removed, volume kept.
stack-down:
	docker compose down

# `down -v` removes the postgres_data volume, that is the database. Then rebuilds and restarts in
# the foreground.
stack-reset:
	docker compose down -v
	docker compose up --build

# On the host. Inserts the three demonstration violations (CTRL-003 amount, CTRL-005 risk score,
# CTRL-006 status), all tagged source_system = 'demo_scenario', then re-runs the controls so that
# those three turn red.
demo-inject:
	python -m banking_dataops.demo_scenario inject
	python -m banking_dataops.quality_checks

# Deletes the rows tagged 'demo_scenario' and nothing else, then re-runs the controls.
demo-reset:
	python -m banking_dataops.demo_scenario reset
	python -m banking_dataops.quality_checks

# Prints how many rows still carry the 'demo_scenario' tag.
demo-status:
	python -m banking_dataops.demo_scenario status

# scripts/smoke_test.sh: containers up, scripts/smoke_check.py inside app (database, gold, model,
# audit), dashboard probed from the host. smoke_check.py prints DEMO READY when its checks pass.
# The stack must already be running. The script is run directly, so it needs its executable bit:
# git stores it as 100644 (run chmod +x scripts/*.sh after a checkout that lost the bit).
smoke:
	./scripts/smoke_test.sh

# scripts/preflight.sh: checks the image exists, restarts the stack from an empty volume (this wipes
# the database), times the cold start against 120 s, then runs the smoke test. For the morning of
# the presentation.
preflight:
	./scripts/preflight.sh

# Same as demo-inject and demo-reset, run inside the app container, for a host with no Python
# environment.
demo-inject-app:
	docker compose exec app python -m banking_dataops.demo_scenario inject
	docker compose exec app python -m banking_dataops.quality_checks

demo-reset-app:
	docker compose exec app python -m banking_dataops.demo_scenario reset
	docker compose exec app python -m banking_dataops.quality_checks

# `$$` is make's escape for `$`: the shell receives ${API_PORT:-8000}. The shell reads the
# environment, not a .env file, so a port set only in .env is not reflected here.
api-open:
	@echo "Verdict   : http://localhost:$${API_PORT:-8000}/verdict"
	@echo "Controls  : http://localhost:$${API_PORT:-8000}/controls"
	@echo "Incidents : http://localhost:$${API_PORT:-8000}/incidents"
	@echo "OpenAPI   : http://localhost:$${API_PORT:-8000}/docs"

# The /verdict an automated pipeline would call. `can_publish` is the field to branch on: a WARN
# verdict still has can_publish true (ADR-0006). Needs the host's curl and Python. Same note on
# API_PORT as api-open.
api-verdict:
	@curl -fsS http://localhost:$${API_PORT:-8000}/verdict | python -m json.tool

# Writes the exact versions installed in the app image to requirements.lock (pip freeze, project
# excluded). Nothing in the repository installs from that file: the Dockerfile reads requirements.txt.
lock:
	docker compose run --rm --entrypoint pip app freeze --exclude-editable > requirements.lock

# Creates the eight vw_* views of the star schema (sql/06_powerbi_star_schema.sql) in the public
# schema, for Power BI to import.
powerbi-views:
	docker compose exec app python -m banking_dataops.powerbi_views

# Reads vw_publication_verdict, one of the views Power BI imports.
powerbi-check:
	docker compose exec postgres psql -U dataops -d banking_dataops -c "SELECT * FROM vw_publication_verdict;"

# The 30 latest rows of audit_events, with the business reason cut at 60 characters.
audit-trail:
	docker compose exec postgres psql -U dataops -d banking_dataops -c \
	"SELECT occurred_at, run_id, category, action, row_count, left(reason, 60) AS reason \
	 FROM audit_events ORDER BY occurred_at DESC LIMIT 30;"

# One row per run from vw_run_summary (sql/07_audit_journal.sql): duration, event count, failures,
# invariant violations and the clean flag.
audit-runs:
	docker compose exec postgres psql -U dataops -d banking_dataops -c \
	"SELECT * FROM vw_run_summary ORDER BY started_at DESC LIMIT 20;"

# Reverse tests (ADR-0007): forbidden paths in test_reverse_paths.py, reproducibility in
# test_reproducibility.py.
reverse-test:
	python -m pytest tests/test_reverse_paths.py tests/test_reproducibility.py -v

# Runs run_pipeline.py in the running app container: Bronze (generation), cleaning, Silver (load and
# the seven controls), Gold. No model: training is pipeline-train, scoring is ml-predict. Safe to
# repeat, the load steps truncate then insert.
pipeline:
	docker compose exec app python run_pipeline.py

# The same run plus model training (--train): writes models/suspicion_classifier.joblib. It does not
# fill gold.predictions: follow with `make ml-predict`.
pipeline-train:
	docker compose exec app python run_pipeline.py --train

# --verify is accepted by run_pipeline.py but adds no check: this is one ordinary run of the
# pipeline, not two, and nothing is compared. Idempotence comes from the TRUNCATE then INSERT of the
# load steps, not from this flag.
pipeline-verify:
	docker compose exec app python run_pipeline.py --verify

# Trains a logistic regression (baseline) and a random forest tuned by grid search, keeps the forest
# only if it beats the baseline by the margin set in train.py, chooses the decision threshold on
# out-of-fold predictions and writes models/suspicion_classifier.joblib (ADR-0008, ADR-0009). Both
# models use balanced class weights. Run on demand: the boot sequence never trains. It does not
# score the batch: that is ml-predict.
ml-train:
	docker compose exec app python -m banking_dataops.ml.train

# Scores every transaction with models/suspicion_classifier.joblib and rewrites gold.predictions
# (truncate then insert, one transaction). priority_band: high from the decision threshold up,
# medium from 0.60 x threshold up to it, low below; the threshold is stored in decision_threshold
# (ADR-0009). Fails if the model file is missing: run ml-train first.
ml-predict:
	docker compose exec app python -m banking_dataops.ml.predict

# Runs the cleaning step alone, in the container: data/ CSV files to data/silver/, quarantining what
# cannot be repaired. Files only, nothing is loaded into the tables. run_pipeline.py runs this step
# itself.
clean-bronze:
	docker compose exec app python -m banking_dataops.pipeline.cleaning

# tests/integration: generation, cleaning and feature building exercised together, with no database
# (see tests/integration/__init__.py).
integration-test:
	python -m pytest tests/integration -v

# Prints the command to open the notebook; it opens nothing.
eda:
	@echo "Notebook: notebooks/01_eda_bronze.ipynb"
	@echo "  jupyter lab notebooks/01_eda_bronze.ipynb"
