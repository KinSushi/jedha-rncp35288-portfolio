# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Réglages communs des tests : à lire avant tout import de jupyter.

jupyter_core 5.x émet un `DeprecationWarning` à l'import tant que la variable
`JUPYTER_PLATFORM_DIRS` n'est pas posée (« Jupyter is migrating its paths to use standard
platformdirs »). Les tests sont lancés avec `-W error::DeprecationWarning` : cet avertissement
ferait échouer la collecte. On pose la variable, comme le demande l'avertissement lui-même, au
lieu de le masquer.
"""

import os

os.environ.setdefault("JUPYTER_PLATFORM_DIRS", "1")
