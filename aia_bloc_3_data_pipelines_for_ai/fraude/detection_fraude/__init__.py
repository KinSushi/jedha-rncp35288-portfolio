# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Détection automatique de fraude : pipeline temps réel, rapport du matin, réentraînement.

Le paquet est organisé en couches, de la source vers le métier :

* ``producteur``      : interroge l'API (ou rejoue le jeu), valide, masque, publie ;
* ``consommateur``    : note chaque transaction, stocke en bronze / argent / or, notifie ;
* ``rapport``         : rapport du matin (lot) construit sur la couche argent ;
* ``entrainement``    : pipeline scikit-learn suivi dans MLflow ;
* ``dags`` (hors paquet) : orchestration Airflow, qui n'appelle que des fonctions du paquet.

CHOIX
-----
- Retenu : une chaîne de version des règles, ``VERSION_REGLES``, distincte de ``__version__``.
  Elle est inscrite dans chaque ligne de la couche argent : si elle change, ``retraitement`` sait
  quoi recalculer.
- Écarté : réutiliser ``__version__`` (version du paquet) pour dater les règles. Alternative non
  documentée dans le dépôt (source : inconnue) ; le choix retenu est celui du code.
- Retenu : un paquet sans effet de bord à l'import (deux constantes seulement), pour que chaque
  module reste importable seul, y compris depuis un DAG Airflow.

OÙ
---
- Place : racine du paquet ``detection_fraude`` ; tous les modules du paquet l'importent
  indirectement.
- Appelants de ``VERSION_REGLES`` : ``consommateur`` (inscription dans l'argent et le lignage),
  ``entrainement`` (métadonnées du modèle), ``rapport`` et ``retraitement`` (lignage).
- Dépendances : aucune.

COMMENT
-------
- ``__version__`` identifie le paquet ; ``VERSION_REGLES`` identifie l'état des règles de
  transformation (masquage, validation, caractéristiques) sous la forme ``AAAA.M.n``.
- Preuve : ``tests/test_retraitement.py`` rejoue une période avec un autre modèle et vérifie la
  version écrite dans l'argent ; ``tests/test_consommateur.py`` vérifie que le lignage porte la
  version des règles.

POURQUOI
--------
- La version des règles permet de savoir « ce qui reste à recalculer » après un changement :
  livrables/aia3/02_pipeline_et_qualite.md:109-117 (cycle de vie et retraitement).
- Définition et commentaire d'origine : detection_fraude/__init__.py:55.
- Test du lignage : test_lignage_du_retraitement (tests/test_retraitement.py:96-99).

JOURNALISATION
--------------
Ce module ne journalise rien : il ne contient que deux constantes. La version des règles
apparaît dans les événements des modules qui l'utilisent (voir leur section JOURNALISATION).
"""

__version__ = "1.0.0"

# Version des règles de transformation (masquage, validation, caractéristiques). Elle est inscrite
# dans chaque ligne de la couche argent : si elle change, ``retraitement`` sait quoi recalculer.
VERSION_REGLES = "2026.10.1"
