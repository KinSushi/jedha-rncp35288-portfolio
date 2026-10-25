# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Génère les secrets de la production locale dans ``secrets/`` (jamais versionné).

Le fichier ``docker-compose.yml`` ne contient aucun mot de passe : chaque secret est un fichier
monté dans le conteneur sous ``/run/secrets/`` (mécanisme des secrets Docker). Ce script crée ces
fichiers avec des valeurs aléatoires fortes, **sans les afficher**, et ne remplace jamais un
secret déjà présent (relancer le script est donc sans danger).

Usage (depuis le dossier fraude/) : ``python scripts/initialiser_secrets.py``

Les secrets facultatifs (SMTP, webhook) sont créés vides : les renseigner pour activer le canal
correspondant. Le mot de passe de l'interface Airflow est dans le fichier
``airflow_admin_motdepasse.txt`` du dossier ``secrets``.

CHOIX
-----
- Retenu : des valeurs générées par le module ``secrets`` (aléatoire cryptographique), en
  hexadécimal pour les mots de passe : rien à échapper dans une URL de connexion ou une instruction
  SQL.
- Retenu : ne jamais écraser un secret existant ; les mots de passe PostgreSQL ne sont réécrits que
  s'ils sont tous absents, sinon la base déjà initialisée et les fichiers de connexion
  divergeraient.
- Retenu : ne rien afficher des valeurs (le script n'imprime que le dossier et l'étape suivante).
- Retenu : fichiers lisibles par tous (0644) car le processus du conteneur (uid 50000) n'est pas le
  propriétaire du fichier sur l'hôte ; dossier réservé au propriétaire (0700).
- Écarté : un coffre de secrets (Vault, gestionnaire du fournisseur) pour la production locale ; il
  est la suite proposée pour une production réelle, l'application lisant déjà ses secrets par
  ``FRAUDE_<NOM>_FICHIER``.

OÙ
---
- Place : script d'amorçage, lancé une fois avant ``docker compose up``.
- Appelants : l'opérateur, à la main ; ses fichiers sont lus par ``docker-compose.yml`` et par
  lire_secret (detection_fraude/configuration.py:93-121).
- Dépendances : bibliothèque standard seulement (``secrets``, ``base64``, ``os``, ``pathlib``).

COMMENT
-------
1. ``main`` crée le dossier (droits 0700), génère les mots de passe PostgreSQL et les chaînes de
   connexion si le premier secret PostgreSQL est absent, puis le sel de hachage, les clés d'Airflow
   et le mot de passe administrateur, puis les secrets facultatifs vides.
2. ``ecrire`` ne crée le fichier que s'il n'existe pas et rend ``True`` seulement s'il l'a créé.
Tests qui le prouvent : test_ecrire_cree_le_fichier_puis_ne_remplace_jamais_un_secret_existant
(tests/test_initialiser_secrets.py:130-147) et
test_main_ne_reecrit_pas_les_secrets_postgres_si_le_superuser_existe_deja
(tests/test_initialiser_secrets.py:274-291).

POURQUOI
--------
- Gestion des secrets, mécanisme unique, rotation et dossier exclu du dépôt :
  livrables/aia3/03_securite_conformite.md:129-156.
- Production locale et secrets Docker : docker-compose.yml:38.
- Protection du dossier par le ``.gitignore`` du dépôt :
  test_le_gitignore_protege_secrets_donnees_et_jeu (tests/test_divers.py:117-126).
- Indicateur 5.2 du référentiel AIA RNCP41993.

JOURNALISATION
--------------
Le script n'utilise pas le journal du paquet. Il imprime deux lignes sur la sortie standard : le
dossier des secrets (« valeurs non affichées ») et l'étape suivante. Aucune valeur secrète n'est
jamais écrite, ni imprimée.
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent.parent / "secrets"


def mot_de_passe() -> str:
    """Hexadécimal : rien à échapper dans une URL de connexion ou une instruction SQL."""
    return secrets.token_hex(24)


def ecrire(nom: str, valeur: str) -> bool:
    """Crée le fichier s'il n'existe pas. Renvoie ``True`` si un secret a été créé."""
    chemin = DOSSIER / nom
    if chemin.exists():
        return False
    chemin.write_text(valeur, encoding="utf-8")
    chemin.chmod(0o644)
    return True


def main() -> int:
    DOSSIER.mkdir(exist_ok=True)
    DOSSIER.chmod(0o700)
    pg = {nom: mot_de_passe() for nom in ("superuser", "airflow", "mlflow", "fraude")}
    # Les mots de passe PostgreSQL ne sont réécrits que s'ils sont tous absents : sinon la
    # base déjà initialisée et les fichiers de connexion divergeraient.
    if not (DOSSIER / "pg_superuser_motdepasse.txt").exists():
        for nom, valeur in pg.items():
            ecrire(f"pg_{nom}_motdepasse.txt", valeur)
        ecrire(
            "dsn_entrepot.txt", f"postgresql+psycopg2://fraude:{pg['fraude']}@postgres:5432/fraude"
        )
        ecrire(
            "airflow_sql_conn.txt",
            f"postgresql+psycopg2://airflow:{pg['airflow']}@postgres:5432/airflow",
        )
        ecrire(
            "mlflow_backend_uri.txt",
            f"postgresql+psycopg2://mlflow:{pg['mlflow']}@postgres:5432/mlflow",
        )
    ecrire("sel_hachage.txt", secrets.token_hex(32))
    ecrire("airflow_fernet_key.txt", base64.urlsafe_b64encode(os.urandom(32)).decode())
    ecrire("airflow_secret_key.txt", secrets.token_hex(32))
    ecrire("airflow_admin_motdepasse.txt", mot_de_passe())
    for facultatif in ("smtp_utilisateur.txt", "smtp_mot_de_passe.txt", "webhook_url.txt"):
        ecrire(facultatif, "")
    print(f"Secrets prêts dans {DOSSIER} (valeurs non affichées).")
    print("Étape suivante : docker compose up -d --build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
