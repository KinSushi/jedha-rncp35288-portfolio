# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Rend le module du projet importable quand on lance `pytest` sans `python -m`.

Le dossier du projet est AJOUTÉ EN FIN de sys.path, jamais en tête : sous mutmut, la copie mutée
du module est placée avant, et une entrée en tête ferait importer l'original (aucun mutant ne
serait alors testé). Avec `python -m pytest`, le dossier courant est déjà dans sys.path.
"""

import sys
from pathlib import Path

RACINE = str(Path(__file__).resolve().parents[1])
if RACINE not in sys.path:
    sys.path.append(RACINE)
