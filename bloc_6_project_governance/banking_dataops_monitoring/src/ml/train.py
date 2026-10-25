"""Training entry point: a re-export, kept so that the path ``src/ml/train.py`` resolves.

CHOIX
-----
Retained: a module that only re-exports. The implementation stays in the ``banking_dataops`` package, because
a package gives namespacing, importability and testability that a loose folder of scripts does not.
Rejected: copying the code here (a second copy would drift), and moving the implementation to match the folder
layout of these paths (the package structure would be lost).

OÙ
---
Role: alias at ``src/ml/train.py``. It re-exports ``TrainingReport``, ``main`` and ``train`` from
``banking_dataops.ml.train``.
Callers: none in the repository; it exists for a reader who looks for this path. It is
importable from the source tree (``pythonpath = ["src"]`` in ``pyproject.toml``) but deliberately not
installed as a top-level package: names as generic as ``ml`` or ``cleaning`` would shadow other packages in a
shared environment (comment of the ``[tool.setuptools.packages.find]`` section of ``pyproject.toml``).
Depends only on ``banking_dataops.ml.train``.

COMMENT
-------
One import and an ``__all__`` list. No logic. The behaviour, and the tests that prove it, are those of the
target module: tests/test_ml_training.py and tests/test_ml.py.

POURQUOI
--------
- The paths src/ingestion/ingest.py, src/cleaning/clean.py, src/transformations/ and src/ml/predict.py
are kept as aliases of the package modules, one entry point per stage (src/README.md).
- Re-export instead of duplicate: a copy of the logic would drift within a week (the original comment of this
file).

JOURNALISATION
--------------
None: this module neither logs nor prints. The logging of the target module applies.
"""

from banking_dataops.ml.train import TrainingReport, main, train

__all__ = ["TrainingReport", "main", "train"]
