# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Sortie console en UTF-8 : une précaution que trois commandes de la démonstration partagent.

CHOIX
-----
- Retenu : une fonction unique, `forcer_utf8()`, appelée en tête de `dimensionnement.main`,
  de `generer_ddl.main` et de `pipeline.executer`. Les trois commandes affichent des accents
  (« débit », « écrit », « États-Unis ») ; sur Windows, la console redirigée vers un fichier ou
  un tube garde l'encodage de la page de code locale (cp1252) et ces accents deviendraient
  illisibles ou lèveraient une erreur d'encodage.
- Retenu : ne reconfigurer que si le flux est un `io.TextIOWrapper`. Sous un test ou un tube
  remplacé par un autre type de flux (un `StringIO`), il n'y a rien à reconfigurer et la commande
  ne doit pas échouer pour autant.
- Écarté : `sys.stdout.reconfigure(...)` écrit tel quel aux trois endroits. `mypy --strict`
  refuse l'appel (`sys.stdout` est un `TextIO`, qui n'a pas de méthode `reconfigure`) et la même
  précaution aurait été écrite trois fois.
- Écarté : la variable d'environnement `PYTHONUTF8=1`. Elle règle le problème pour tout
  l'interpréteur, mais il faudrait que chaque personne qui lance la démonstration l'exporte ;
  le code ne peut pas l'imposer de l'intérieur.

OÙ
---
- Place : `stripe_demo/console.py`, module de la couche « interface en ligne de commande ».
- Appelants : `stripe_demo/dimensionnement.py` (fonction `main`), `stripe_demo/generer_ddl.py`
  (fonction `main`) et `stripe_demo/pipeline.py` (fonction `executer`).
- Dépendances : bibliothèque standard seule (`io`, `sys`). Aucun état n'est conservé.

COMMENT
-------
- `forcer_utf8()` lit `sys.stdout` ; si c'est un `io.TextIOWrapper`, elle appelle sa méthode
  `reconfigure(encoding="utf-8")`, qui change l'encodage du flux ouvert sans le fermer.
- Les tests qui le prouvent (`tests/test_console.py`) : un flux ouvert en cp1252 est bien en
  `utf-8` après l'appel ; un accent écrit ensuite sort en deux octets UTF-8 ; un flux qui n'est
  pas un `TextIOWrapper`, ou l'absence de sortie standard, est laissé tel quel, sans erreur.

POURQUOI
--------
- Mesuré le 2026-10-11 sur cette machine : un processus Python dont la sortie est un tube annonce
  `sys.stdout.encoding` = cp1252 (mode UTF-8 inactif), donc les accents des trois commandes sont
  mal codés sans cette précaution.
- Les trois points d'appel sont `stripe_demo/dimensionnement.py:158`,
  `stripe_demo/generer_ddl.py:533` et `stripe_demo/pipeline.py:97`.
- Source de la méthode `reconfigure` : documentation de la bibliothèque standard Python
  (`io.TextIOWrapper.reconfigure`), source à joindre ; non rouverte dans cette session.

JOURNALISATION
--------------
- Aucune : le module ne journalise rien et n'écrit rien lui-même. Il change seulement
  l'encodage de la sortie standard ; les messages que les commandes y écrivent ensuite sont
  ceux de ces commandes.
"""

from __future__ import annotations

import io
import sys


def forcer_utf8() -> None:
    """Met la sortie standard en UTF-8 quand c'est possible, sans rien faire sinon."""
    flux = sys.stdout
    if isinstance(flux, io.TextIOWrapper):
        flux.reconfigure(encoding="utf-8")
