# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Alimentation d'un vrai PostgreSQL (« production locale ») avec les données synthétiques.

Pourquoi ce module : la démonstration `pipeline.py` est autonome (SQLite + DuckDB). Pour montrer
la capture des changements telle qu'elle existe en production, il faut un PostgreSQL dont le
journal de transactions (WAL) est lu par Debezium. Ce module réutilise les MÊMES fonctions de
génération que la démonstration (`oltp.referentiels`, `oltp.paiements_synthetiques`) ; seul le
moteur change.

Deux commandes, qui n'utilisent pas le même compte (moindre privilège) :

- ``amorcer`` : référentiels (pays, devises, taux, marchands, clients…), avec le compte
  propriétaire ; à exécuter une fois ;
- ``flux`` : lots de nouveaux paiements, puis changements de statut, avec le compte applicatif
  `svc_application`, qui n'a aucun droit de suppression ni de modification des montants.

Aucun mot de passe n'est lu dans le code : ils viennent de fichiers de secrets (variables
``..._FILE``, convention des images Docker) ou de variables d'environnement.

Usage : ``python -m stripe_demo.vers_postgres amorcer`` puis ``... flux --lots 20 --pause 3``.

CHOIX
-----
- Retenu : réutiliser les MÊMES générateurs que la démonstration autonome (`oltp.referentiels`,
  `oltp.paiements_synthetiques`), seul le moteur change : les chiffres des deux modes restent
  comparables.
- Retenu : deux commandes, deux comptes (moindre privilège) : `amorcer` avec le compte
  propriétaire, `flux` avec `svc_application`, qui n'a ni suppression ni modification des montants.
- Retenu : aucun mot de passe par défaut, ni dans le code ni vide : le secret vient d'un fichier
  (`<NOM>_FILE`, convention des images Docker) ou d'une variable ; son absence arrête la commande.
  Écarté : un mot de passe de démonstration, qui serait public.
- Retenu : `amorcer` est idempotent (rien à faire si les marchands existent déjà).
- Retenu : le fichier de secret vide est refusé, comme l'absence : un mot de passe vide ne doit
  pas passer pour un secret valide.

OÙ
---
- Place : couche « production locale » (Docker Compose), en face de `stripe_demo/pipeline.py`
  (démonstration autonome). Appelants : les services `amorcage` et `demonstration` de
  `docker-compose.yml`, les tests `tests/test_vers_postgres.py`.
- Dépendances : SQLAlchemy et `psycopg` 3, `stripe_demo/oltp.py`, les variables d'environnement
  `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER` et les secrets.

COMMENT
-------
- `lire_secret` lit `<NOM>_FILE` (contenu sans espaces de bord) sinon `<NOM>` ; `moteur` construit
  l'URL `postgresql+psycopg` et le moteur (`pool_pre_ping`) ; `amorcer` charge les référentiels si
  la base est vide ; `flux` insère `lots` lots de `par_lot` paiements, conteste un paiement
  réussi récent par lot (événements INSERT puis UPDATE pour la capture des changements) et dort
  `pause_s` secondes entre deux lots ; `main` lit la ligne de commande.
- Ce qui le prouve (`tests/test_vers_postgres.py`) : secrets (fichier, variable, priorité,
  absent, vide, introuvable), URL construite avec un faux `create_engine`, amorçage et flux
  exécutés sur SQLite (les requêtes utilisées y sont valides) avec horloge et pauses factices,
  ligne de commande et refus. Un PostgreSQL réel n'est exercé que par les tests Docker optionnels.

POURQUOI
--------
- La « production locale » (Compose, services, comptes) : `livrables/aia2/05_pipeline.md:159-177`.
- Moindre privilège du compte applicatif : `livrables/aia2/02_oltp_erd.md:269-280`.
- Secrets en fichiers `/run/secrets` et jamais de mot de passe écrit : `docker-compose.yml:1-30`.

JOURNALISATION
--------------
- Pas de module `logging` : `amorcer` et `flux` affichent un compte rendu par lot sur la sortie
  standard (`flush=True`, pour le suivre en direct dans le conteneur). Un secret manquant lève
  `SystemExit` avec son message ; aucune exception n'est avalée.
"""

from __future__ import annotations

import argparse
import os
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine

from stripe_demo import oltp


def lire_secret(nom: str) -> str:
    """Lit un secret : fichier désigné par ``<NOM>_FILE``, sinon variable ``<NOM>``.

    Échoue explicitement s'il est absent ou vide : jamais de mot de passe par défaut, ni de mot
    de passe vide (un fichier de secret vide ne passe plus pour un secret valide).
    """
    fichier = os.environ.get(f"{nom}_FILE")
    valeur: str | None
    if fichier:
        valeur = Path(fichier).read_text(encoding="utf-8").strip()
    else:
        valeur = os.environ.get(nom)  # absent : None, refusé comme un secret vide ci-dessous
    if not valeur:
        raise SystemExit(f"secret manquant : définir {nom}_FILE ou {nom}")
    return valeur


def moteur(utilisateur: str, secret: str) -> Engine:
    """Moteur PostgreSQL (psycopg 3) ; hôte, port et base viennent de l'environnement."""
    url = URL.create(
        "postgresql+psycopg", username=utilisateur, password=lire_secret(secret),
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        database=os.environ.get("POSTGRES_DB", "stripe"))
    return create_engine(url, pool_pre_ping=True)


def amorcer(engine: Engine, jours: int = 14) -> None:
    """Charge les référentiels si la base est vide (idempotent : relançable sans effet)."""
    with engine.connect() as cx:
        if cx.execute(text("SELECT count(*) FROM marchands")).scalar_one() > 0:
            print("référentiels déjà présents : rien à faire")
            return
    oltp.referentiels(engine, jours=jours)
    print(f"référentiels chargés (taux de change sur {jours} jours)")


def flux(engine: Engine, lots: int, par_lot: int, pause_s: float, graine: int) -> None:
    """Insère des lots de paiements, puis conteste quelques-uns (événements INSERT et UPDATE)."""
    alea = random.Random(graine)
    with engine.connect() as cx:
        dernier = cx.execute(text("SELECT coalesce(max(id_paiement), 0) FROM paiements")
                             ).scalar_one()
    for lot in range(1, lots + 1):
        debut = datetime.now(UTC) - timedelta(days=1)
        dernier = oltp.paiements_synthetiques(engine, debut, par_lot, graine=graine * 1000 + lot,
                                              premier_id=dernier + 1)
        with engine.begin() as cx:
            # Rétrofacturation : un paiement réussi récent devient « conteste » (UPDATE capturé).
            cible = cx.execute(text(
                "SELECT id_paiement FROM paiements WHERE statut = 'reussi' "
                "AND id_paiement > :borne ORDER BY id_paiement LIMIT 1 OFFSET :decalage"),
                {"borne": dernier - par_lot, "decalage": alea.randint(0, par_lot // 2)}
            ).scalar()
            if cible is not None:
                cx.execute(text("UPDATE paiements SET statut = 'conteste' WHERE id_paiement = :i"),
                           {"i": cible})
        print(f"lot {lot}/{lots} : {par_lot} paiements insérés (dernier id {dernier})",
              flush=True)
        if lot < lots:
            time.sleep(pause_s)


def main(argv: list[str] | None = None) -> None:
    parseur = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sous = parseur.add_subparsers(dest="commande", required=True)
    sous.add_parser("amorcer", help="charger les référentiels (compte propriétaire)")
    f = sous.add_parser("flux", help="insérer des lots de paiements (compte applicatif)")
    f.add_argument("--lots", type=int, default=20)
    f.add_argument("--par-lot", type=int, default=50)
    f.add_argument("--pause", type=float, default=3.0, help="secondes entre deux lots")
    f.add_argument("--graine", type=int, default=2026)
    args = parseur.parse_args(argv)
    if args.commande == "amorcer":
        amorcer(moteur(os.environ.get("POSTGRES_USER", "postgres"), "POSTGRES_PASSWORD"))
    else:
        flux(moteur("svc_application", "POSTGRES_APP_PASSWORD"), args.lots, args.par_lot,
             args.pause, args.graine)


if __name__ == "__main__":
    main()
