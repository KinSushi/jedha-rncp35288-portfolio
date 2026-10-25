# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Prépare les tests : module du projet importable, calcul sur un seul fil.

Le dossier du projet est AJOUTÉ EN FIN de sys.path, jamais en tête : sous mutmut, la copie mutée
du module est placée avant, et une entrée en tête ferait importer l'original (aucun mutant ne
serait alors testé). Avec `python -m pytest`, le dossier courant est déjà dans sys.path.

Un seul fil BLAS/OpenMP, comme dans le notebook (OMP_NUM_THREADS=1) : MiniBatchKMeans donne ainsi
les mêmes centres d'une machine à l'autre, et les tests qui comparent deux ajustements au bit
près ne dépendent pas du nombre de cœurs.
"""

import sys
from pathlib import Path

import pytest
from threadpoolctl import threadpool_limits

RACINE = str(Path(__file__).resolve().parents[1])
if RACINE not in sys.path:
    sys.path.append(RACINE)


@pytest.fixture(scope="session", autouse=True)
def un_seul_fil_de_calcul():
    """Limite BLAS et OpenMP à un fil pour toute la session de tests."""
    with threadpool_limits(limits=1):
        yield
