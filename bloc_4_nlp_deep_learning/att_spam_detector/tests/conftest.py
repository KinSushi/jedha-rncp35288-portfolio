# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Configuration commune des tests : un seul fil de calcul pour des résultats reproductibles."""

import os

import pytest
import torch

# Le tokeniseur (Rust) alerte ou se bloque s'il a déjà travaillé en parallèle avant un fork
# (mutmut lance chaque mutant dans un processus fils) : on le garde sur un seul fil.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


@pytest.fixture(autouse=True, scope="session")
def un_seul_fil_de_calcul() -> None:
    """Un fil de calcul : résultats identiques d'une machine à l'autre et tests rapides."""
    torch.set_num_threads(1)
