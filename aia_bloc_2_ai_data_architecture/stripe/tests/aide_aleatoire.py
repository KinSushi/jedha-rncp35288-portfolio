# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Aide aux tests : un faux générateur aléatoire qui note ce qu'on lui demande.

Pourquoi : les données de la démonstration sont tirées au hasard avec une graine fixe. Un test
sur des tirages ne dit pas si les BORNES déclarées (durée de session entre 5 et 900 secondes,
identifiant client entre 1 et 2000...) sont respectées : un tirage ne tombe presque jamais sur
une borne. On remplace donc `random.Random` par un espion qui (1) enregistre chaque appel avec
ses arguments, et (2) rend toujours une valeur extrême choisie par le test. Le test lit alors
les documents produits aux bornes et la liste exacte des intervalles demandés.
"""

from __future__ import annotations

import math


class AleaEspion:
    """Remplace `random.Random(graine)` : s'appelle comme la classe, rend toujours le même objet.

    - `randint` : `"max"` rend la borne haute, `"min"` la borne basse ;
    - `choix` : position rendue par `choice`, `choices` et `sample` (0 = premier, -1 = dernier) ;
    - `reponses` : réponses imposées à `randint(a, b)` pour un couple (a, b) précis (une liste,
      consommée dans l'ordre) ; sert à forcer une valeur intermédiaire.
    """

    def __init__(self, randint: str = "max", choix: int = 0, reponses=None):
        assert randint in ("min", "max")
        self.randint_mode = randint
        self.choix = choix
        self.reponses = {cle: list(valeurs) for cle, valeurs in (reponses or {}).items()}
        self.graines: list[object] = []
        self.appels: list[tuple] = []

    def __call__(self, graine=None):
        self.graines.append(graine)
        return self

    def noter(self, nom, *args, **kwargs):
        """Enregistre un appel (nom, arguments positionnels, arguments nommés triés)."""
        self.appels.append((nom, args, tuple(sorted(kwargs.items()))))

    def randint(self, a, b):
        self.noter("randint", a, b)
        if self.reponses.get((a, b)):
            return self.reponses[(a, b)].pop(0)
        return b if self.randint_mode == "max" else a

    def _position(self, taille: int) -> int:
        """Position demandée, ramenée dans la séquence (les petites listes n'ont pas de 4e)."""
        return self.choix if self.choix < 0 else min(self.choix, taille - 1)

    def choice(self, sequence):
        self.noter("choice", tuple(sequence))
        return sequence[self._position(len(sequence))]

    def choices(self, population, weights=None, *, k=1):
        self.noter("choices", tuple(population), None if weights is None else tuple(weights),
                    k=k)
        return [population[self._position(len(population))]] * k

    def sample(self, population, k):
        self.noter("sample", tuple(population), k=k)
        liste = list(population)
        return liste[-k:] if self.choix < 0 else liste[:k]

    def random(self):
        self.noter("random")
        return 0.999999 if self.randint_mode == "max" else 0.0

    def uniform(self, a, b):
        self.noter("uniform", a, b)
        return b if self.randint_mode == "max" else a

    def lognormvariate(self, mu, sigma):
        self.noter("lognormvariate", mu, sigma)
        return math.exp(mu)

    def appels_de(self, nom: str) -> list[tuple]:
        """Arguments (positionnels, puis nommés) de tous les appels à la méthode `nom`."""
        return [(args, nommes) for n, args, nommes in self.appels if n == nom]
