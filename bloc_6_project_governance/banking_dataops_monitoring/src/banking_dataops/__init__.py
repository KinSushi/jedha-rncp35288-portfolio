"""Banking DataOps Monitoring: a batch pipeline that decides whether a bank's data batch can be published.

The package holds the whole system: a synthetic data generator, cleaning, the PostgreSQL load, seven
deterministic controls and their verdict, a reconciliation summary, the Gold layer, a prioritisation model, a
read-only API and the helpers behind the Streamlit dashboard.

CHOIX
-----
Retained: one importable package, ``banking_dataops``, with one module per concern, instead of a folder of
scripts. It gives namespacing, importability and testability. The sub-packages are ``pipeline`` (cleaning and
Gold build) and ``ml`` (features, training, prediction). The package is installed with ``pip install -e .``
(``pyproject.toml``) and the version below is the package metadata version.
Rejected: a flat folder of scripts, and installing the spec-path aliases (``src/ingestion``, ``src/cleaning``,
``src/transformations``, ``src/ml``) as top-level packages (see ``src/ml/predict.py``).

OÙ
---
Role: the root of the code under ``src/``. Callers: ``run_pipeline.py``, ``dashboard/streamlit_app.py``,
``scripts/`` and the tests import its modules. Its dependencies are listed in ``pyproject.toml`` and
``requirements.txt``. The layers, in the order a batch crosses them: ``generate_synthetic_data`` (Bronze),
``pipeline.cleaning``, ``ingest`` (Silver), ``quality_checks`` and ``reconciliation``, ``pipeline.gold`` and
``powerbi_views``, ``ml.train`` and ``ml.predict``; ``api``, ``monitoring`` and ``incident_report`` read the
result; ``audit``, ``logging_config``, ``config`` and ``db`` serve every layer.

COMMENT
-------
This file defines only ``__version__``. Importing the package imports nothing else, so a module can be loaded on
its own. The architecture is described in docs/architecture.md and docs/pipeline_architecture.md.
Tests that prove it: tests/test_config.py (``test_requirements_txt_matches_pyproject``) checks the declared
dependencies; every other test file exercises one module.

POURQUOI
--------
- The decisions behind the layers are recorded as ADRs in docs/adr/: batch rather than stream (ADR-0001),
  deterministic rules rather than a model for blocking (ADR-0002), synthetic data (ADR-0003), constraints in the
  database and rules in the controls (ADR-0004), local containers (ADR-0005), a read-only API (ADR-0006), audit
  and reverse tests (ADR-0007), a prioritisation classifier (ADR-0008) and selection outside the test set
  (ADR-0009).
- The value of ``__version__`` is 0.1.0, as in ``pyproject.toml``. The CHANGELOG records releases of the project
  with other numbers; the relation between the two is not documented: source : inconnue.

JOURNALISATION
--------------
None: this file neither logs nor prints.
"""

__version__ = "0.1.0"
