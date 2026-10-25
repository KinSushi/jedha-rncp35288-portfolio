-- Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
-- Fichier GÉNÉRÉ par `python -m stripe_demo.generer_ddl` depuis la spécification TABLES_OLAP.
-- Ne pas modifier à la main : modifier la spécification puis relancer le générateur.
-- Variante portable (sans options Redshift), exécutée par les tests dans DuckDB.

CREATE TABLE IF NOT EXISTS dim_date (
    cle_date INTEGER NOT NULL,
    "date" DATE NOT NULL,
    annee INTEGER NOT NULL,
    mois INTEGER NOT NULL,
    jour INTEGER NOT NULL,
    jour_semaine INTEGER NOT NULL,
    est_weekend BOOLEAN NOT NULL,
    PRIMARY KEY (cle_date)
);

CREATE TABLE IF NOT EXISTS dim_geographie (
    code_pays VARCHAR(2) NOT NULL,
    nom VARCHAR(80) NOT NULL,
    region VARCHAR(20) NOT NULL,
    PRIMARY KEY (code_pays)
);

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
);

CREATE TABLE IF NOT EXISTS dim_client (
    cle_client BIGINT NOT NULL,
    code_pays VARCHAR(2) NOT NULL,
    anciennete_jours INTEGER NOT NULL,
    segment VARCHAR(12) NOT NULL,
    PRIMARY KEY (cle_client)
);

CREATE TABLE IF NOT EXISTS dim_moyen (
    cle_moyen BIGINT NOT NULL,
    "type" VARCHAR(12) NOT NULL,
    marque VARCHAR(12),
    pays_emission VARCHAR(2),
    PRIMARY KEY (cle_moyen)
);

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
);

-- Agrégats pré-calculés : les tableaux de bord lisent peu de lignes.

CREATE VIEW agr_chiffre_quotidien AS
SELECT d."date" AS "date", f.cle_marchand, COUNT(*) AS nb_paiements,
       SUM(CASE WHEN f.est_reussi THEN f.montant_usd END) AS chiffre_usd,
       AVG(CASE WHEN f.est_rembourse THEN 1.0 ELSE 0.0 END) AS taux_remboursement
FROM faits_paiements f JOIN dim_date d ON d.cle_date = f.cle_date
GROUP BY d."date", f.cle_marchand;

CREATE VIEW agr_fraude_pays AS
SELECT f.pays_ip, COUNT(*) AS nb_paiements,
       AVG(CASE WHEN f.est_conteste THEN 1.0 ELSE 0.0 END) AS taux_litige
FROM faits_paiements f
GROUP BY f.pays_ip;
