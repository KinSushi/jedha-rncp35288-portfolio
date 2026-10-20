# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Entrepôt analytique (OLAP) en étoile, chargé en batch puis au fil de l'eau (CDC).

En production : Redshift Serverless (ou Snowflake) alimenté par Airflow (batch quotidien) et
par le flux Kafka (micro-batchs). En démonstration : DuckDB, moteur analytique en colonnes
embarqué, qui exécute le même SQL standard.

Schéma en étoile (grain de la table de faits : UN paiement) :
- `faits_paiements` : mesures (montant en USD, indicateurs de succès, remboursement, litige,
  score de risque) et clés vers les dimensions ;
- dimensions : `dim_date`, `dim_marchand` (historisée, type 2 : on garde l'ancienne version
  quand un marchand change de statut), `dim_client`, `dim_moyen`, `dim_geographie` ;
- agrégats pré-calculés (`agr_chiffre_quotidien`, `agr_fraude_pays`) : les tableaux de bord
  lisent quelques milliers de lignes au lieu de balayer tous les faits.

CHOIX
-----
- Retenu : une étoile (faits au grain « un paiement », cinq dimensions) plutôt qu'un flocon : les
  dimensions sont petites devant les faits et la redondance est sans effet sur le volume.
- Retenu : DuckDB lit directement la base SQLite par l'extension `sqlite` (ATTACH en lecture
  seule), l'équivalent local d'une extraction Airflow. Écarté : recopier l'OLTP par un export
  intermédiaire.
- Retenu : `INSERT OR REPLACE` sur `id_paiement` pour le flux : un changement rejoué remplace la
  ligne, sans doublon (équivalent du `MERGE` de production).
- Retenu : chaque chargement est une transaction (tout ou rien) et la base OLTP est détachée même
  en cas d'erreur. Écarté : l'ancien déroulé sans transaction, qui vidait les tables avant de
  les recharger (un chemin OLTP erroné laissait un entrepôt vide) et laissait `oltp` attachée
  (tous les appels suivants échouaient).
- Limite connue, non corrigée : aucun changement de marchand n'est traité en type 2 ; toutes
  les lignes sont chargées comme courantes (note 03).

OÙ
---
- Place : couche « entrepôt » de la démonstration. Appelants : `stripe_demo/pipeline.py`
  (`connecter`, `charger_batch`, `appliquer_changements`) et les tests `tests/test_olap.py`,
  `tests/test_cdc.py`, `tests/test_olap_chargeurs.py`.
- Dépendances : DuckDB et son extension `sqlite` ; les tables de l'OLTP (`stripe_demo/oltp.py`) ;
  `stripe_demo/generer_ddl.py` vérifie que `DDL_ETOILE` reste conforme à `TABLES_OLAP`.

COMMENT
-------
- `connecter` ouvre l'entrepôt et crée les six tables ; `charger_batch` vide puis recharge les
  dimensions et les faits, puis reconstruit les agrégats ; `inserer_faits` ajoute d'abord les
  jours manquants à `dim_date` (sans eux, un paiement reçu un jour nouveau disparaîtrait des
  agrégats) puis insère les faits filtrés ; `appliquer_changements` lit le journal des changements
  après une position et rend (nouvelle position, nombre de faits ajoutés).
- Ce qui le prouve : `tests/test_olap.py` (aucun paiement perdu ni créé, montants au taux du jour,
  agrégats égaux aux faits), `tests/test_cdc.py` (rejeu, reprise, mise à jour de statut) et
  `tests/test_olap_chargeurs.py` (ordre exact des instructions, reprise sur erreur, refus d'un
  chemin introuvable ou d'une position qui n'est pas un nombre).

POURQUOI
--------
- Étoile, mesures, historisation et agrégats : `livrables/aia2/03_olap_schema.md:83-134`.
- Chargement et défaut corrigé sur `dim_date` : `livrables/aia2/03_olap_schema.md:123` et
  `livrables/aia2/03_olap_schema.md:150-161`.
- Idempotence du consommateur, rejeu « au moins une fois » :
  `livrables/aia2/05_pipeline.md:105-114`.

JOURNALISATION
--------------
- Rien n'est journalisé (pas de module `logging`). Une erreur pendant un chargement annule la
  transaction, détache l'OLTP et se propage à l'appelant (`duckdb.Error`, `ValueError` pour une
  position qui n'est pas un nombre) ; aucune exception n'est avalée.
"""

from __future__ import annotations

import duckdb

DDL_ETOILE = """
CREATE TABLE IF NOT EXISTS dim_date (
    cle_date INTEGER PRIMARY KEY, date DATE, annee INTEGER, mois INTEGER, jour INTEGER,
    jour_semaine INTEGER, est_weekend BOOLEAN);
CREATE TABLE IF NOT EXISTS dim_marchand (
    cle_marchand INTEGER PRIMARY KEY, id_marchand BIGINT, raison_sociale VARCHAR,
    code_pays VARCHAR, code_mcc VARCHAR, statut VARCHAR,
    valide_du TIMESTAMP, valide_au TIMESTAMP, est_courant BOOLEAN);
CREATE TABLE IF NOT EXISTS dim_client (
    cle_client BIGINT PRIMARY KEY, code_pays VARCHAR, anciennete_jours INTEGER, segment VARCHAR);
CREATE TABLE IF NOT EXISTS dim_moyen (
    cle_moyen BIGINT PRIMARY KEY, type VARCHAR, marque VARCHAR, pays_emission VARCHAR);
CREATE TABLE IF NOT EXISTS dim_geographie (
    code_pays VARCHAR PRIMARY KEY, nom VARCHAR, region VARCHAR);
CREATE TABLE IF NOT EXISTS faits_paiements (
    id_paiement BIGINT PRIMARY KEY, cle_date INTEGER, cle_marchand INTEGER, cle_client BIGINT,
    cle_moyen BIGINT, pays_ip VARCHAR, code_devise VARCHAR, appareil VARCHAR, heure INTEGER,
    montant_usd DOUBLE, est_reussi BOOLEAN, est_rembourse BOOLEAN, est_conteste BOOLEAN,
    score_risque DOUBLE);
"""


def connecter(chemin: str) -> duckdb.DuckDBPyConnection:
    """Ouvre l'entrepôt et crée le schéma en étoile s'il n'existe pas."""
    cx = duckdb.connect(chemin)
    cx.execute(DDL_ETOILE)
    return cx


def charger_batch(cx: duckdb.DuckDBPyConnection, chemin_oltp: str) -> dict[str, int]:
    """Chargement complet (batch nocturne) depuis l'OLTP : dimensions puis faits.

    DuckDB lit directement la base SQLite (extension sqlite) : c'est l'équivalent local d'une
    extraction par Airflow. Le chargement est idempotent (tables vidées puis rechargées).
    """
    cx.execute("INSTALL sqlite; LOAD sqlite;")
    cx.execute(f"ATTACH '{chemin_oltp}' AS oltp (TYPE sqlite, READ_ONLY)")
    cx.begin()
    try:
        for table in ("faits_paiements", "dim_date", "dim_marchand", "dim_client", "dim_moyen",
                      "dim_geographie"):
            cx.execute(f"DELETE FROM {table}")
        cx.execute("""
            INSERT INTO dim_geographie SELECT code_pays, nom, region FROM oltp.pays;
            INSERT INTO dim_marchand
                SELECT row_number() OVER (ORDER BY id_marchand), id_marchand, raison_sociale,
                       code_pays, code_mcc, statut, cree_le::TIMESTAMP, NULL, TRUE
                FROM oltp.marchands;
            INSERT INTO dim_moyen
                SELECT id_moyen, type, marque, pays_emission FROM oltp.moyens_paiement;
            INSERT INTO dim_date
                SELECT CAST(strftime(d, '%Y%m%d') AS INTEGER), d, year(d), month(d), day(d),
                       isodow(d), isodow(d) >= 6
                FROM (SELECT DISTINCT CAST(cree_le AS DATE) AS d FROM oltp.paiements);
        """)
        cx.execute("""
            INSERT INTO dim_client
            SELECT c.id_client, c.code_pays,
                   date_diff('day', CAST(c.cree_le AS DATE), DATE '2026-09-30'),
                   CASE WHEN count(p.id_paiement) >= 30 THEN 'fidele'
                        WHEN count(p.id_paiement) >= 10 THEN 'regulier' ELSE 'occasionnel' END
            FROM oltp.clients c LEFT JOIN oltp.paiements p USING (id_client)
            GROUP BY c.id_client, c.code_pays, c.cree_le
        """)
        n = inserer_faits(cx, "TRUE")
        construire_agregats(cx)
        cx.commit()
    except BaseException:
        # Tout ou rien : les tables ont été vidées avant d'être rechargées ; sans annulation,
        # une erreur au milieu (chemin OLTP erroné, table absente) laisserait un entrepôt vide.
        cx.rollback()
        raise
    finally:
        # Détaché même en cas d'erreur : une base restée attachée ferait échouer tous les
        # appels suivants (« database with name oltp already exists »).
        cx.execute("DETACH oltp")
    return {"faits": n}


def inserer_faits(cx: duckdb.DuckDBPyConnection, filtre: str) -> int:
    """Insère (ou remplace) les faits des paiements de l'OLTP qui satisfont « filtre ».

    Conversion en USD au taux du jour du paiement (table de référence `taux_change`).

    Les jours encore absents de `dim_date` y sont ajoutés d'abord : sans cela, un paiement reçu
    par la CDC un jour nouveau aurait une clé de date orpheline et disparaîtrait de tous les
    agrégats, qui joignent la dimension de date. Au chargement batch, la dimension est déjà
    complète et cette étape n'ajoute rien.
    """
    avant = cx.execute("SELECT count(*) FROM faits_paiements").fetchall()[0][0]
    cx.execute(f"""
        INSERT INTO dim_date
        SELECT CAST(strftime(d, '%Y%m%d') AS INTEGER), d, year(d), month(d), day(d),
               isodow(d), isodow(d) >= 6
        FROM (SELECT DISTINCT CAST(p.cree_le AS DATE) AS d FROM oltp.paiements p WHERE {filtre})
        WHERE CAST(strftime(d, '%Y%m%d') AS INTEGER) NOT IN (SELECT cle_date FROM dim_date)
    """)
    cx.execute(f"""
        INSERT OR REPLACE INTO faits_paiements
        SELECT p.id_paiement, CAST(strftime(CAST(p.cree_le AS DATE), '%Y%m%d') AS INTEGER),
               dm.cle_marchand, p.id_client, p.id_moyen, p.pays_ip, p.code_devise, p.appareil,
               hour(CAST(p.cree_le AS TIMESTAMP)),
               p.montant_mineur / 100.0 * t.taux_vers_usd,
               p.statut = 'reussi', p.statut = 'rembourse', p.statut = 'conteste',
               p.score_risque
        FROM oltp.paiements p
        JOIN dim_marchand dm ON dm.id_marchand = p.id_marchand AND dm.est_courant
        JOIN oltp.taux_change t ON t.code_devise = p.code_devise
                               AND t.date_taux = CAST(p.cree_le AS DATE)
        WHERE {filtre}
    """)
    return int(cx.execute("SELECT count(*) FROM faits_paiements").fetchall()[0][0] - avant)


def construire_agregats(cx: duckdb.DuckDBPyConnection) -> None:
    """Tables d'agrégats (l'équivalent des vues matérialisées de Redshift)."""
    cx.execute("""
        CREATE OR REPLACE TABLE agr_chiffre_quotidien AS
        SELECT d.date, f.cle_marchand, count(*) AS nb_paiements,
               sum(f.montant_usd) FILTER (WHERE f.est_reussi) AS chiffre_usd,
               avg(f.est_rembourse::INTEGER) AS taux_remboursement
        FROM faits_paiements f JOIN dim_date d USING (cle_date)
        GROUP BY d.date, f.cle_marchand;
        CREATE OR REPLACE TABLE agr_fraude_pays AS
        SELECT f.pays_ip, count(*) AS nb_paiements, avg(f.est_conteste::INTEGER) AS taux_litige
        FROM faits_paiements f GROUP BY f.pays_ip;
    """)


def appliquer_changements(cx: duckdb.DuckDBPyConnection, chemin_oltp: str,
                          position: int) -> tuple[int, int]:
    """Consommateur CDC : applique les changements de l'OLTP depuis la position donnée.

    Renvoie (nouvelle position, nombre de faits insérés ou mis à jour). Comme un consommateur
    Kafka qui valide son offset, on mémorise la dernière position traitée : une relance reprend
    au bon endroit, et un changement rejoué remplace la ligne (INSERT OR REPLACE) sans doublon.
    """
    cx.execute("INSTALL sqlite; LOAD sqlite;")
    cx.execute(f"ATTACH '{chemin_oltp}' AS oltp (TYPE sqlite, READ_ONLY)")
    cx.begin()
    try:
        derniere = cx.execute(
            "SELECT coalesce(max(position), ?) FROM oltp.journal_changements", [position]
        ).fetchall()[0][0]
        ids = f"""p.id_paiement IN (SELECT id_ligne FROM oltp.journal_changements
                    WHERE table_source = 'paiements' AND position > {int(position)}
                      AND position <= {int(derniere)})"""
        n = inserer_faits(cx, ids)
        construire_agregats(cx)
        cx.commit()
    except BaseException:
        cx.rollback()  # voir charger_batch : un micro-lot s'applique en entier ou pas du tout
        raise
    finally:
        cx.execute("DETACH oltp")  # voir charger_batch : jamais de base restée attachée
    return derniere, n
