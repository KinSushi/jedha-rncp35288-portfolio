# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Aide aux tests : charge `deploiement/generer_secrets.py`, qui n'est pas un paquet Python."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

CHEMIN = Path(__file__).resolve().parent.parent / "deploiement" / "generer_secrets.py"


def charger_generer_secrets() -> ModuleType:
    specification = importlib.util.spec_from_file_location("generer_secrets", CHEMIN)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module
