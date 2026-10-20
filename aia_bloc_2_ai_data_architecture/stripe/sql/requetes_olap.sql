-- Copyright © 2026 · voir le fichier LICENSE du dépôt
-- Requêtes analytiques sur l'entrepôt en étoile (SQL standard : DuckDB en démonstration,
-- Redshift en production). Chaque bloc commence par un commentaire « arobase » suivi
-- de la question métier.

-- @ Chiffre d'affaires mensuel (USD) et nombre de paiements réussis
SELECT d.annee, d.mois, round(sum(f.montant_usd), 2) AS chiffre_usd, count(*) AS nb_paiements
FROM faits_paiements f JOIN dim_date d USING (cle_date)
WHERE f.est_reussi
GROUP BY d.annee, d.mois ORDER BY d.annee, d.mois;

-- @ Les 5 marchands au plus fort chiffre (agrégat pré-calculé)
SELECT m.raison_sociale, round(sum(a.chiffre_usd), 2) AS chiffre_usd
FROM agr_chiffre_quotidien a JOIN dim_marchand m USING (cle_marchand)
GROUP BY m.raison_sociale ORDER BY chiffre_usd DESC LIMIT 5;

-- @ Taux de remboursement et de litige par catégorie d'activité (MCC)
SELECT m.code_mcc, count(*) AS nb_paiements,
       round(avg(f.est_rembourse::INTEGER) * 100, 2) AS taux_remboursement_pct,
       round(avg(f.est_conteste::INTEGER) * 100, 2) AS taux_litige_pct
FROM faits_paiements f JOIN dim_marchand m USING (cle_marchand)
GROUP BY m.code_mcc ORDER BY taux_litige_pct DESC;

-- @ Fraude : taux de litige quand le pays de l'IP diffère du pays du client
SELECT (f.pays_ip <> c.code_pays) AS pays_different, count(*) AS nb_paiements,
       round(avg(f.est_conteste::INTEGER) * 100, 2) AS taux_litige_pct
FROM faits_paiements f JOIN dim_client c USING (cle_client)
GROUP BY pays_different ORDER BY pays_different;

-- @ Fraude par tranche horaire (nuit 0-6 h contre journée)
SELECT CASE WHEN f.heure < 6 THEN 'nuit (0-6 h)' ELSE 'journée' END AS tranche,
       count(*) AS nb_paiements, round(avg(f.est_conteste::INTEGER) * 100, 2) AS taux_litige_pct
FROM faits_paiements f GROUP BY tranche ORDER BY tranche;

-- @ Segmentation des clients (dimension client) : volume et panier moyen
SELECT c.segment, count(DISTINCT f.cle_client) AS clients, count(*) AS paiements,
       round(avg(f.montant_usd), 2) AS panier_moyen_usd
FROM faits_paiements f JOIN dim_client c USING (cle_client)
GROUP BY c.segment ORDER BY paiements DESC;

-- @ Série temporelle : chiffre quotidien et moyenne mobile sur 7 jours (fonction fenêtre)
SELECT date, round(chiffre, 2) AS chiffre_usd,
       round(avg(chiffre) OVER (ORDER BY date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 2)
           AS moyenne_mobile_7j
FROM (SELECT date, sum(chiffre_usd) AS chiffre FROM agr_chiffre_quotidien GROUP BY date)
ORDER BY date DESC LIMIT 7;
