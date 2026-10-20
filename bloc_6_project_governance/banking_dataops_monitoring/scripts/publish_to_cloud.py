#!/usr/bin/env python3
"""Publie le résultat du pipeline vers une base PostgreSQL cloud (Neon / Supabase).

Le script recopie le lot courant vers une base managée gratuite, puis y recrée le schéma en étoile : Power BI se
connecte
ensuite au cloud.

CHOIX
-----
Retenu :

- Recopier le lot vers une base managée gratuite. Power BI Desktop ne sait pas atteindre un PostgreSQL qui tourne dans
  un
  conteneur sur le poste du présentateur, et un rapport publié sur Power BI Service doit lire une base joignable depuis
  Internet.
- `COPY` au format CSV plutôt que binaire : le CSV reste lisible entre deux versions majeures de PostgreSQL, ce qui est
  le
  cas courant entre un poste local et une base managée.
- Une publication ponctuelle, à relancer après chaque exécution du pipeline dont on veut publier le résultat : chaque
  table
  de la cible est vidée (`TRUNCATE ... CASCADE`) puis remplie, pour qu'elle reflète exactement la source.
- `sslmode=require` ajouté quand la chaîne n'en porte pas : Neon et Supabase refusent les connexions en clair, et mieux
  vaut
  le dire ici que laisser psycopg échouer sur un message obscur.
- Le mot de passe de la cible n'est jamais affiché : seule la chaîne masquée (`mask`) l'est.

Écarté :

- Une réplication continue : le script publie un lot, il ne réplique pas. Raison du choix : source : inconnue.
- Le mode DirectQuery de Power BI : le script conseille le mode Import, les vues étant peu volumineuses.

OÙ
---
Rôle : script d'exploitation, hors du pipeline, lancé à la main après `run_pipeline.py`. Il lit la base du projet
(réglages
de `banking_dataops.config`) et écrit dans la base désignée par `BANKING_DATAOPS_CLOUD_DSN`. Dépendances : psycopg,
`banking_dataops.config.load_settings`, `banking_dataops.db.split_sql_statements` et les fichiers `sql/00_schema.sql`,
`sql/07_audit_journal.sql`, `sql/08_gold_layer.sql` et `sql/06_powerbi_star_schema.sql`. La page du Space Hugging Face
(`huggingface-space/app.py`) lit ensuite la base cible en lecture seule.

COMMENT
-------
1. `target_dsn` lit `BANKING_DATAOPS_CLOUD_DSN` ; absente ou vide, le script s'arrête avec un exemple pour chaque
   fournisseur.
2. Le DDL est appliqué sur la cible (`DDL_FILES`), puis chaque table est recopiée (`SILVER_TABLES` puis `GOLD_TABLES`)
   par
   `COPY ... CSV`, de la source vers la cible, dans l'ordre des clés étrangères : un parent avant ses enfants.
3. Les huit vues du schéma en étoile sont recréées sur la cible, le nombre de lignes de chaque table est comparé des
   deux
   côtés, et le verdict de publication de la cible est affiché.
4. `--dry-run` n'écrit rien et liste ce qui serait copié ; `--verify` compare seulement les volumes. Le code de sortie
   est 0
   si tout est identique, 1 dès qu'un écart est trouvé.

    export BANKING_DATAOPS_CLOUD_DSN="postgresql://user:pwd@host/db?sslmode=require"
    python scripts/publish_to_cloud.py            # publie
    python scripts/publish_to_cloud.py --dry-run  # vérifie sans écrire
    python scripts/publish_to_cloud.py --verify   # compare seulement les volumes

Tests qui le prouvent : tests/test_publish_to_cloud.py, sur des bases simulées
(``test_target_dsn_refuses_a_missing_variable``,
``test_dry_run_lists_what_would_be_copied_and_writes_nothing``,
``test_publication_copies_every_table_in_the_documented_order_and_reports_the_verdict``,
``test_publication_never_prints_the_password_of_the_target``,
``test_publication_exits_with_a_failure_when_a_copied_table_does_not_match``).

POURQUOI
--------
- L'ordre des tables est imposé par les clés étrangères de `sql/00_schema.sql` et `sql/08_gold_layer.sql`.
- Le verdict de la cible vient de `vw_publication_verdict`, la même vue que celle de la source : le verdict de
  publication
  ne dépend que des contrôles déterministes (ADR-0002).
- Neon et Supabase refusent les connexions en clair : source à joindre (documentation des deux fournisseurs, non
  versionnée
  dans ce dépôt).

JOURNALISATION
--------------
Aucun module `logging` : le script imprime sur la sortie standard chaque étape, chaque table avec son nombre de lignes
et
chaque écart (`ERR`). Le mot de passe de la cible n'est jamais imprimé : seule la chaîne masquée l'est.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"
sys.path.insert(0, str(ROOT / "src"))

from banking_dataops.config import load_settings  # noqa: E402
from banking_dataops.db import split_sql_statements  # noqa: E402

# Ordre imposé par les clés étrangères : un parent avant ses enfants.
SILVER_TABLES = [
    "customers",
    "accounts",
    "transactions",
    "quality_check_results",
    "incident_reports",
    "reconciliation_results",
    "audit_events",
]
GOLD_TABLES = [
    "gold.dim_date",
    "gold.dim_customer",
    "gold.dim_account",
    "gold.dim_control",
    "gold.fact_transactions",
    "gold.fact_quality_results",
    "gold.predictions",
]

# DDL rejouable, dans l'ordre où il doit être appliqué sur une base vierge.
DDL_FILES = ["00_schema.sql", "07_audit_journal.sql", "08_gold_layer.sql"]
VIEWS_FILE = "06_powerbi_star_schema.sql"


def mask(dsn: str) -> str:
    """Masque le mot de passe pour l'affichage."""
    dsn = re.sub(r"(://[^:/@]+:)[^@]+(@)", r"\1***\2", dsn)
    return re.sub(r"(password=)\S+", r"\1***", dsn)


def target_dsn() -> str:
    dsn = os.environ.get("BANKING_DATAOPS_CLOUD_DSN", "").strip()
    if not dsn:
        sys.exit(
            "BANKING_DATAOPS_CLOUD_DSN n'est pas défini.\n"
            "Exemple Neon     : postgresql://user:pwd@ep-xxx.eu-central-1.aws.neon.tech/"
            "banking_dataops?sslmode=require\n"
            "Exemple Supabase : postgresql://postgres.xxxx:pwd@aws-0-eu-central-1."
            "pooler.supabase.com:5432/postgres?sslmode=require"
        )
    if "sslmode" not in dsn:
        # Neon et Supabase refusent les connexions en clair. Mieux vaut le dire
        # ici que laisser psycopg échouer sur un message obscur.
        dsn += ("&" if "?" in dsn else "?") + "sslmode=require"
        print("  note : sslmode=require ajouté automatiquement")
    return dsn


def apply_sql_file(conn: psycopg.Connection, path: Path, label: str) -> None:
    """Applique un fichier SQL instruction par instruction."""
    statements = split_sql_statements(path.read_text(encoding="utf-8"))
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)
    conn.commit()
    print(f"  ok   {label:<28} ({len(statements)} instructions)")


def copy_table(src: psycopg.Connection, dst: psycopg.Connection, table: str) -> int:
    """Recopie une table via COPY CSV.

    Le format CSV est choisi plutôt que binaire : il reste lisible entre deux
    versions majeures de PostgreSQL différentes, ce qui est le cas courant entre
    un poste local et une base managée.
    """
    buffer = io.StringIO()
    with src.cursor().copy(f"COPY {table} TO STDOUT (FORMAT CSV)") as reader:
        for block in reader:
            buffer.write(bytes(block).decode("utf-8"))
    payload = buffer.getvalue()

    with dst.cursor() as cur:
        cur.execute(f"TRUNCATE {table} CASCADE")
        with cur.copy(f"COPY {table} FROM STDIN (FORMAT CSV)") as writer:
            writer.write(payload)
    dst.commit()
    return payload.count("\n")


def count_rows(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        row = cur.fetchone()
        assert row is not None, "COUNT(*) always returns one row"
        return int(row[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="n'écrit rien sur la cible")
    parser.add_argument("--verify", action="store_true", help="compare seulement les volumes")
    args = parser.parse_args()

    settings = load_settings()
    dsn_out = target_dsn()

    print("=" * 66)
    print("  PUBLICATION VERS UNE BASE POSTGRESQL CLOUD")
    print("=" * 66)
    print(f"  source : {settings.database_name}@{settings.database_host}:{settings.database_port}")
    print(f"  cible  : {mask(dsn_out)}")
    print()

    with psycopg.connect(settings.dsn) as src, psycopg.connect(dsn_out) as dst:
        with dst.cursor() as cur:
            cur.execute("SELECT version()")
            version_row = cur.fetchone()
            assert version_row is not None, "SELECT version() always returns one row"
            print(f"  cible atteinte : {version_row[0].split(',')[0]}")
        print()

        if args.verify:
            print("VÉRIFICATION DES VOLUMES")
            ok = True
            for table in SILVER_TABLES + GOLD_TABLES:
                try:
                    a, b = count_rows(src, table), count_rows(dst, table)
                except psycopg.Error as exc:
                    print(f"  ERR  {table:<30} {str(exc).splitlines()[0]}")
                    dst.rollback()
                    ok = False
                    continue
                flag = "ok " if a == b else "ERR"
                ok &= a == b
                print(f"  {flag}  {table:<30} source={a:<8} cible={b}")
            print()
            print("  volumes identiques." if ok else "  ÉCART DÉTECTÉ.")
            return 0 if ok else 1

        if args.dry_run:
            print("MODE DRY-RUN — aucune écriture")
            for table in SILVER_TABLES + GOLD_TABLES:
                print(f"  copierait  {table:<30} {count_rows(src, table):>8} lignes")
            return 0

        print("1. SCHÉMA")
        for name in DDL_FILES:
            apply_sql_file(dst, SQL_DIR / name, name)
        print()

        print("2. DONNÉES")
        total = 0
        for table in SILVER_TABLES + GOLD_TABLES:
            rows = copy_table(src, dst, table)
            total += rows
            print(f"  ok   {table:<30} {rows:>8} lignes")
        print()

        print("3. SCHÉMA EN ÉTOILE (vues Power BI)")
        apply_sql_file(dst, SQL_DIR / VIEWS_FILE, VIEWS_FILE)
        print()

        print("4. CONTRÔLE")
        mismatches = 0
        for table in SILVER_TABLES + GOLD_TABLES:
            a, b = count_rows(src, table), count_rows(dst, table)
            if a != b:
                mismatches += 1
                print(f"  ERR  {table:<30} source={a} cible={b}")
        with dst.cursor() as cur:
            cur.execute("SELECT verdict, can_publish FROM vw_publication_verdict")
            verdict = cur.fetchone()
        assert verdict is not None, "vw_publication_verdict always has one row"
        print(f"  ok   verdict publié sur la cible : {verdict[0]} (can_publish={verdict[1]})")
        print()

        print("=" * 66)
        if mismatches:
            print(f"  ÉCHEC — {mismatches} table(s) en écart")
            return 1
        print(f"  PUBLIÉ — {total} lignes, 8 vues, verdict {verdict[0]}")
        print("=" * 66)
        print()
        print("  Power BI Desktop : Obtenir les données > Base de données PostgreSQL")
        print("  Serveur  : l'hôte de la chaîne ci-dessus (port inclus)")
        print("  Base     : le nom de base de la chaîne")
        print("  Connect. : Import (pas DirectQuery — les vues sont peu volumineuses)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
