# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Surveillance du retard des emplacements de réplication logique (la requête du document 10).

Pourquoi ce module : quand le connecteur Debezium s'arrête, PostgreSQL GARDE tout le journal de
transactions (WAL) que l'emplacement de réplication n'a pas encore confirmé. Le disque se
remplit en silence jusqu'à arrêter la base (document 10, § 3, panne n° 4). La requête qui
mesure ce retard existait dans le document ; elle devient ici une commande exécutable, avec un
code de sortie que n'importe quel ordonnanceur (cron, Airflow, tâche ECS) sait lire.

Usage : ``python -m stripe_demo.supervision --seuil-octets 134217728``
(128 Mio : une heure de WAL à la pointe du scénario S2, hypothèse H17 du document 09).

CHOIX
-----
- Retenu : la requête exacte du document 10, validée sur PostgreSQL 16 (`pg_wal_lsn_diff` entre la
  position courante du WAL et la dernière position confirmée par le consommateur).
- Retenu : deux causes d'alerte. Un emplacement INACTIF (personne ne le lit : connecteur arrêté),
  quel que soit son retard ; un emplacement actif dont le retard dépasse le seuil (connecteur
  trop lent). Écarté : n'alerter que sur le seuil, qui laisserait passer un connecteur arrêté
  juste après un redémarrage, retard encore faible mais qui ne baissera plus.
- Retenu : seuil OBLIGATOIRE sur la ligne de commande, sans valeur par défaut cachée dans le
  code : c'est une hypothèse d'exploitation (H17), elle doit se lire là où on lance la commande.
- Retenu : codes de sortie 0 (rien à signaler), 1 (au moins une alerte), 2 (ligne de commande
  invalide, code d'argparse). En production, la même mesure vient de la métrique CloudWatch
  `OldestReplicationSlotLag` (alarme du module Terraform `supervision`) ; cette commande sert au
  diagnostic de l'astreinte et en « production locale » (Docker Compose).

OÙ
---
- Place : outil d'exploitation à côté de `stripe_demo/vers_postgres.py`, dont il réutilise la
  connexion (`moteur` : hôte, port et base dans l'environnement, secret dans un fichier).
- Appelants : l'astreinte (procédure du document 10 § 3), un ordonnanceur ; les tests
  `tests/test_supervision.py` et, sur un vrai PostgreSQL, `tests/test_postgres_docker.py`.
- Dépendances : SQLAlchemy et `psycopg` 3 (par `vers_postgres.moteur`).

COMMENT
-------
- `lire_emplacements` exécute `REQUETE_RETARD` et rend une liste d'`Emplacement` (nom, actif,
  retard en octets ; retard `None` pour un emplacement sans position confirmée, par exemple
  physique) ; `evaluer` rend les messages d'alerte ; `main` lit la ligne de commande, affiche un
  état par emplacement puis les alertes, et rend le code de sortie.
- Ce qui le prouve (`tests/test_supervision.py`) : seuil franchi ou non (égalité comprise),
  emplacement inactif, retard inconnu, seuil nul ou négatif refusé, requête transmise telle
  quelle et identique au bloc SQL du document 10, conversion des lignes, `Emplacement` non
  modifiable, sortie affichée et codes de sortie 0, 1 et 2, y compris par `python -m` ; sur
  PostgreSQL 16 réel (test Docker optionnel), un emplacement laissé sans lecteur est signalé.

POURQUOI
--------
- La requête et la procédure (connecteur arrêté, WAL retenu) :
  `livrables/aia2/10_continuite_supervision_maintenance.md:38` et
  `livrables/aia2/10_continuite_supervision_maintenance.md:58-67`.
- Le seuil (une heure de WAL à la pointe en S2) : hypothèse H17,
  `livrables/aia2/09_dimensionnement_finops_greenops.md:59`.

JOURNALISATION
--------------
- Pas de module `logging` : la commande écrit sur la sortie standard, une ligne par emplacement
  puis une ligne « ALERTE » par problème, pour être lue par l'astreinte ou par un ordonnanceur.
  Une erreur de connexion se propage (`sqlalchemy.exc.OperationalError`) ; rien n'est avalé.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine

from stripe_demo import vers_postgres

REQUETE_RETARD = """SELECT slot_name, active,
       pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS retard_octets
FROM pg_replication_slots"""


@dataclass(frozen=True)
class Emplacement:
    """Un emplacement de réplication tel que PostgreSQL le décrit."""

    nom: str
    actif: bool
    retard_octets: int | None


def lire_emplacements(engine: Engine) -> list[Emplacement]:
    """Exécute la requête de retard et convertit chaque ligne."""
    with engine.connect() as cx:
        lignes = cx.execute(text(REQUETE_RETARD)).all()
    return [Emplacement(str(nom), bool(actif), None if retard is None else int(retard))
            for nom, actif, retard in lignes]


def evaluer(emplacements: Sequence[Emplacement], seuil_octets: int) -> list[str]:
    """Messages d'alerte : emplacement inactif, ou retard strictement au-delà du seuil."""
    if seuil_octets <= 0:
        raise ValueError("le seuil doit être un nombre d'octets strictement positif")
    alertes = []
    for e in emplacements:
        if not e.actif:
            alertes.append(f"{e.nom} : aucun lecteur (connecteur arrêté), "
                           f"WAL retenu : {e.retard_octets} octets")
        elif e.retard_octets is not None and e.retard_octets > seuil_octets:
            alertes.append(f"{e.nom} : retard de {e.retard_octets} octets, "
                           f"au-delà du seuil de {seuil_octets}")
    return alertes


def lire_depuis_postgres(utilisateur: str, secret: str) -> list[Emplacement]:
    """Lecture sur le PostgreSQL désigné par l'environnement (voir `vers_postgres.moteur`)."""
    engine = vers_postgres.moteur(utilisateur, secret)
    try:
        return lire_emplacements(engine)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None,
         lecteur: Callable[[str, str], list[Emplacement]] = lire_depuis_postgres) -> int:
    parseur = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parseur.add_argument("--seuil-octets", type=int, required=True,
                         help="WAL retenu au-delà duquel alerter (hypothèse H17)")
    parseur.add_argument("--utilisateur", default=os.environ.get("POSTGRES_USER", "postgres"),
                         help="compte de lecture (en production : un compte membre de pg_monitor)")
    parseur.add_argument("--secret", default="POSTGRES_PASSWORD",
                         help="nom du secret du compte (<NOM>_FILE ou <NOM>)")
    args = parseur.parse_args(argv)
    if args.seuil_octets <= 0:
        parseur.error("--seuil-octets doit être strictement positif")
    emplacements = lecteur(args.utilisateur, args.secret)
    if not emplacements:
        print("aucun emplacement de réplication : rien à surveiller")
    for e in emplacements:
        print(f"{e.nom} : actif={e.actif}, retard_octets={e.retard_octets}")
    alertes = evaluer(emplacements, args.seuil_octets)
    for message in alertes:
        print(f"ALERTE {message}")
    return 1 if alertes else 0


if __name__ == "__main__":
    raise SystemExit(main())
