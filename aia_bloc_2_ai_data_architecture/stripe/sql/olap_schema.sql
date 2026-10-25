-- Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
-- Fichier GÉNÉRÉ par `python -m stripe_demo.generer_ddl` depuis la spécification TABLES_OLAP.
-- Ne pas modifier à la main : modifier la spécification puis relancer le générateur.
-- Cible : Amazon Redshift (Serverless). Les contraintes PRIMARY KEY et FOREIGN KEY y sont
-- informatives (non vérifiées) : elles guident l'optimiseur ; l'unicité est garantie par le
-- MERGE de chargement ci-dessous. Montants en DECIMAL (exactitude) ; la démonstration
-- DuckDB utilise DOUBLE. Ce DDL n'a pas été exécuté sur Redshift ; sa variante portable
-- (sans options Redshift) l'est dans DuckDB par les tests.

-- Distribution : les faits et la dimension client partagent la clé cle_client, ce qui
-- co-localise leur jointure ; les petites dimensions sont copiées sur tous les nœuds
-- (DISTSTYLE ALL). Tri : cle_date, car presque toutes les requêtes bornent la période.

CREATE TABLE IF NOT EXISTS dim_date (
    cle_date INTEGER NOT NULL,
    "date" DATE NOT NULL,
    annee INTEGER NOT NULL,
    mois INTEGER NOT NULL,
    jour INTEGER NOT NULL,
    jour_semaine INTEGER NOT NULL,
    est_weekend BOOLEAN NOT NULL,
    PRIMARY KEY (cle_date)
)
DISTSTYLE ALL
SORTKEY (cle_date);

CREATE TABLE IF NOT EXISTS dim_geographie (
    code_pays VARCHAR(2) NOT NULL,
    nom VARCHAR(80) NOT NULL,
    region VARCHAR(20) NOT NULL,
    PRIMARY KEY (code_pays)
)
DISTSTYLE ALL
SORTKEY (code_pays);

CREATE TABLE IF NOT EXISTS dim_marchand (
    cle_marchand INTEGER NOT NULL,
    id_marchand BIGINT NOT NULL,
    raison_sociale VARCHAR(120) NOT NULL,
    code_pays VARCHAR(2) NOT NULL,
    code_mcc VARCHAR(4) NOT NULL,
    statut VARCHAR(12) NOT NULL,
    valide_du TIMESTAMP NOT NULL,
    valide_au TIMESTAMP,
    est_courant BOOLEAN NOT NULL,
    PRIMARY KEY (cle_marchand)
)
DISTSTYLE ALL
SORTKEY (id_marchand, valide_du);

CREATE TABLE IF NOT EXISTS dim_client (
    cle_client BIGINT NOT NULL,
    code_pays VARCHAR(2) NOT NULL,
    anciennete_jours INTEGER NOT NULL,
    segment VARCHAR(12) NOT NULL,
    PRIMARY KEY (cle_client)
)
DISTSTYLE KEY DISTKEY (cle_client)
SORTKEY (cle_client);

CREATE TABLE IF NOT EXISTS dim_moyen (
    cle_moyen BIGINT NOT NULL,
    "type" VARCHAR(12) NOT NULL,
    marque VARCHAR(12),
    pays_emission VARCHAR(2),
    PRIMARY KEY (cle_moyen)
)
DISTSTYLE AUTO
SORTKEY AUTO;

CREATE TABLE IF NOT EXISTS faits_paiements (
    id_paiement BIGINT NOT NULL,
    cle_date INTEGER NOT NULL,
    cle_marchand INTEGER NOT NULL,
    cle_client BIGINT NOT NULL,
    cle_moyen BIGINT NOT NULL,
    pays_ip VARCHAR(2),
    code_devise VARCHAR(3) NOT NULL,
    appareil VARCHAR(10) NOT NULL,
    heure INTEGER NOT NULL,
    montant_usd DECIMAL(18,4) NOT NULL,
    est_reussi BOOLEAN NOT NULL,
    est_rembourse BOOLEAN NOT NULL,
    est_conteste BOOLEAN NOT NULL,
    score_risque DECIMAL(5,4),
    PRIMARY KEY (id_paiement),
    FOREIGN KEY (cle_date) REFERENCES dim_date (cle_date),
    FOREIGN KEY (cle_marchand) REFERENCES dim_marchand (cle_marchand),
    FOREIGN KEY (cle_client) REFERENCES dim_client (cle_client),
    FOREIGN KEY (cle_moyen) REFERENCES dim_moyen (cle_moyen),
    FOREIGN KEY (pays_ip) REFERENCES dim_geographie (code_pays)
)
DISTSTYLE KEY DISTKEY (cle_client)
SORTKEY (cle_date);

-- Agrégats pré-calculés : les tableaux de bord lisent peu de lignes.

CREATE MATERIALIZED VIEW agr_chiffre_quotidien AUTO REFRESH YES AS
SELECT d."date" AS "date", f.cle_marchand, COUNT(*) AS nb_paiements,
       SUM(CASE WHEN f.est_reussi THEN f.montant_usd END) AS chiffre_usd,
       AVG(CASE WHEN f.est_rembourse THEN 1.0 ELSE 0.0 END) AS taux_remboursement
FROM faits_paiements f JOIN dim_date d ON d.cle_date = f.cle_date
GROUP BY d."date", f.cle_marchand;

CREATE MATERIALIZED VIEW agr_fraude_pays AUTO REFRESH YES AS
SELECT f.pays_ip, COUNT(*) AS nb_paiements,
       AVG(CASE WHEN f.est_conteste THEN 1.0 ELSE 0.0 END) AS taux_litige
FROM faits_paiements f
GROUP BY f.pays_ip;

-- Table d'étape : le job de chargement y dépose chaque micro-lot (COPY depuis
-- le lac S3, format Parquet), puis applique le MERGE ci-dessous. Rejouer un lot
-- (reprise après panne) remplace les mêmes lignes : aucun doublon.
CREATE TABLE IF NOT EXISTS stg_faits_paiements (LIKE faits_paiements);

-- Procédure type (à exécuter par le job de chargement, après le COPY) :
-- MERGE INTO faits_paiements
-- USING stg_faits_paiements s ON faits_paiements.id_paiement = s.id_paiement
-- WHEN MATCHED THEN UPDATE SET
--         cle_date = s.cle_date,
--         cle_marchand = s.cle_marchand,
--         cle_client = s.cle_client,
--         cle_moyen = s.cle_moyen,
--         pays_ip = s.pays_ip,
--         code_devise = s.code_devise,
--         appareil = s.appareil,
--         heure = s.heure,
--         montant_usd = s.montant_usd,
--         est_reussi = s.est_reussi,
--         est_rembourse = s.est_rembourse,
--         est_conteste = s.est_conteste,
--         score_risque = s.score_risque
-- WHEN NOT MATCHED THEN INSERT (
--     id_paiement, cle_date, cle_marchand, cle_client, cle_moyen, pays_ip,
--     code_devise, appareil, heure, montant_usd, est_reussi, est_rembourse,
--     est_conteste, score_risque
-- )
-- VALUES (
--     s.id_paiement, s.cle_date, s.cle_marchand, s.cle_client, s.cle_moyen,
--     s.pays_ip, s.code_devise, s.appareil, s.heure, s.montant_usd,
--     s.est_reussi, s.est_rembourse, s.est_conteste, s.score_risque
-- );

-- Rôles de lecture (à exécuter une seule fois) : analystes et data science lisent
-- l'étoile ; aucun des deux n'a accès à l'OLTP.
CREATE ROLE role_analyste;
CREATE ROLE role_data_science;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ROLE role_analyste;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ROLE role_data_science;
