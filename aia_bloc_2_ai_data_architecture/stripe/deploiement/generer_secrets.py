# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Génère les mots de passe de la démonstration locale dans `secrets/` (ignoré par Git).

Pourquoi un script plutôt que des valeurs par défaut : un mot de passe écrit dans un dépôt est
un mot de passe public. Chaque exécution crée des valeurs aléatoires (module `secrets`, source
cryptographique du système) ; un fichier déjà présent n'est jamais écrasé, pour ne pas casser
une base déjà initialisée avec l'ancien mot de passe.

En production, ces secrets n'existent pas sous forme de fichiers : ils vivent dans AWS Secrets
Manager (voir terraform/modules/secrets) et sont lus à l'exécution.

Usage (depuis stripe/) : ``python deploiement/generer_secrets.py``

CHOIX
-----
- Retenu : un script qui crée des valeurs aléatoires au premier lancement, plutôt que des valeurs
  par défaut dans le dépôt : un mot de passe écrit dans un dépôt est un mot de passe public.
- Retenu : le module `secrets` (source cryptographique du système) et 24 octets en base 64
  « URL » : 192 bits, 32 caractères, sans caractère à protéger dans un fichier, une URL ou un
  shell.
- Retenu : ne JAMAIS écraser un secret existant : une base déjà initialisée avec l'ancien mot de
  passe deviendrait inaccessible.
- Retenu : fichiers en lecture ouverte (0o644) pour les utilisateurs non privilégiés des
  conteneurs (UID 10001, kafka) : compromis acceptable pour un poste de développement,
  inadmissible ailleurs. Écarté : les secrets du poste de développement en production ; il y
  existe AWS Secrets Manager.

OÙ
---
- Place : script de démarrage de la « production locale ». Il est lancé à la main avant
  `docker compose up`, qui monte les quatre fichiers en secrets Docker (`/run/secrets`).
  Appelants : l'utilisateur ; les tests `tests/test_generer_secrets.py` et
  `tests/test_deploiement.py`.
- Dépendances : bibliothèque standard seule (`secrets`, `pathlib`, `sys`). Écrit dans `secrets/`
  (dossier ignoré par Git).

COMMENT
-------
- `generer(dossier)` crée le dossier et ses parents au besoin, puis, pour chacun des quatre noms de
  `NOMS`, écrit `<nom>.txt` seulement s'il n'existe pas, fixe ses droits à 0o644 et rend la liste
  des noms créés. Le fichier est écrit en UTF-8, sans conversion de fin de ligne.
- Ce qui le prouve (`tests/test_generer_secrets.py`) : quatre secrets distincts de 32 caractères
  en base 64 « URL », droits 0o644, aucune fin de ligne, dossier imbriqué créé ; côté refus,
  un secret existant n'est jamais écrasé (les manquants sont créés quand même), relancer ne
  change rien, un chemin qui est un fichier est refusé.

POURQUOI
--------
- Les secrets locaux sont des fichiers ignorés par Git, lus par les services via `/run/secrets` :
  `livrables/aia2/05_pipeline.md:159-165`.
- En production, les secrets vivent dans AWS Secrets Manager (module Terraform `secrets`) :
  `terraform/modules/secrets/main.tf:1-20`.

JOURNALISATION
--------------
- Pas de module `logging` : la commande affiche une ligne (« créés : ... » ou « aucun (tous
  existent déjà) ») et rend le code 0. Les valeurs des secrets ne sont JAMAIS affichées ni
  journalisées.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

NOMS = ("postgres_password", "postgres_app_password", "postgres_cdc_password",
        "mongo_root_password")
DOSSIER = Path(__file__).resolve().parent.parent / "secrets"


def generer(dossier: Path = DOSSIER) -> list[str]:
    """Crée les fichiers manquants ; renvoie les noms de ceux qui viennent d'être créés."""
    dossier.mkdir(parents=True, exist_ok=True)
    crees = []
    for nom in NOMS:
        chemin = dossier / f"{nom}.txt"
        if chemin.exists():
            continue
        # 24 octets aléatoires, en base 64 « URL » : 192 bits, sans caractère à protéger.
        chemin.write_text(secrets.token_urlsafe(24), encoding="utf-8", newline="")
        # Lecture ouverte aux utilisateurs non privilégiés des conteneurs (UID 10001, kafka…) :
        # compromis acceptable pour un poste de développement, inadmissible ailleurs.
        chemin.chmod(0o644)
        crees.append(nom)
    return crees


if __name__ == "__main__":
    crees = generer()
    print("créés : " + (", ".join(crees) if crees else "aucun (tous existent déjà)"))
    sys.exit(0)
