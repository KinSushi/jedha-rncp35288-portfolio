-- Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
-- Requêtes d'exemple pour l'entrepôt Kayak
-- Schéma : villes(id_ville, ville, latitude, longitude, temp_max_moy_c, pluie_totale_mm, ensoleillement_moy_h, proba_pluie_moy_pct, score_meteo, rang_meteo)
--          meteo_quotidienne(id_ville, date, temp_max_c, temp_min_c, pluie_mm, proba_pluie_pct, ensoleillement_h, vent_max_kmh, source)
--          hotels(hotel_id, id_ville, nom, url, latitude, longitude, adresse, note, nb_avis, etoiles, description, date_collecte, source, distance_km, commune, hors_commune)
--          vue_hotels_destinations : hôtels joints à la météo de leur ville (prête pour les équipes métier)
-- Notes de lecture :
--   * Ces requêtes sont écrites en SQL standard, pour SQLite (entrepôt local) comme pour
--     PostgreSQL (RDS) : elles ont été exécutées sur SQLite, mais leur exécution sur PostgreSQL
--     n'a pas pu être vérifiée ici (aucune instance). hors_commune est un booléen : valeur 1 ou
--     0 sous SQLite, true ou false sous PostgreSQL, d'où le test CASE WHEN h.hors_commune.
--   * Les hôtels d'une destination sont ceux de son « aire de destination » : un rayon de 8 km
--     autour de son centre. distance_km est la distance de l'hôtel à ce centre. commune est la
--     commune déclarée par OpenStreetMap (addr:city, renseignée pour une minorité d'hôtels).
--     hors_commune vaut vrai si cette commune diffère de la destination, faux si elle est la
--     même, et reste NULL quand la comparaison est impossible (commune absente, ou destination
--     qui n'est pas une commune : massif, département, monument). Une commune déléguée ou
--     associée (Seynod pour Annecy, Lomme pour Lille) compte comme une autre commune : faux
--     positif connu du marquage.
--   * Le lac contient deux collectes. L'entrepôt est chargé avec la collecte COMPLÈTE du
--     2026-10-11 : tous les hôtels nommés d'OpenStreetMap dans l'aire de destination. La
--     première collecte (2026-10-10, requête Overpass plafonnée à 40 éléments par ville) est
--     une archive, qui n'alimente pas l'entrepôt.

-- 1. Top 5 destinations selon le score météo
SELECT ville, score_meteo, rang_meteo, temp_max_moy_c, pluie_totale_mm, ensoleillement_moy_h
FROM villes
ORDER BY rang_meteo ASC
LIMIT 5;

-- 2. Top 20 hôtels des 5 meilleures destinations (note client, puis étoiles, puis nombre d'avis, puis proximité du centre)
WITH top5_villes AS (
    SELECT id_ville
    FROM villes
    ORDER BY rang_meteo ASC
    LIMIT 5
)
SELECT h.nom, h.note, h.nb_avis, h.etoiles, v.ville, h.distance_km, h.commune, h.hors_commune, h.url
FROM hotels h
JOIN top5_villes t ON h.id_ville = t.id_ville
JOIN villes v ON h.id_ville = v.id_ville
ORDER BY h.note DESC NULLS LAST, h.etoiles DESC NULLS LAST, h.nb_avis DESC NULLS LAST,
         h.distance_km ASC NULLS LAST, h.nom ASC, h.hotel_id ASC
LIMIT 20;

-- 3. Nombre d'hôtels et note moyenne par ville
SELECT v.ville,
       COUNT(h.hotel_id) AS nb_hotels,
       AVG(h.note) AS note_moyenne,
       AVG(h.etoiles) AS etoiles_moyennes
FROM villes v
LEFT JOIN hotels h ON v.id_ville = h.id_ville
GROUP BY v.id_ville, v.ville
ORDER BY nb_hotels DESC, note_moyenne DESC;

-- 4. Prévision jour par jour pour une ville donnée (exemple : Paris)
SELECT m.date, m.temp_max_c, m.temp_min_c, m.pluie_mm, m.proba_pluie_pct, m.ensoleillement_h, m.vent_max_kmh
FROM meteo_quotidienne m
JOIN villes v ON m.id_ville = v.id_ville
WHERE v.ville = 'Paris'
ORDER BY m.date;

-- 5. Hôtels sans note (note NULL)
SELECT h.hotel_id, h.nom, v.ville, h.url
FROM hotels h
JOIN villes v ON h.id_ville = v.id_ville
WHERE h.note IS NULL;

-- 6. Contrôle d'intégrité des clés : hôtels référençant une ville inexistante
SELECT h.hotel_id, h.id_ville
FROM hotels h
LEFT JOIN villes v ON h.id_ville = v.id_ville
WHERE v.id_ville IS NULL;

-- 7. Provenance des hôtels (Booking.com ou OpenStreetMap) : traçabilité de la collecte
SELECT source, COUNT(*) AS nb_hotels, SUM(CASE WHEN etoiles IS NOT NULL THEN 1 ELSE 0 END) AS avec_etoiles
FROM hotels
GROUP BY source;

-- 8. Aire de destination : hôtels d'une autre commune que la destination, par ville (communes renseignées seulement)
SELECT v.ville,
       COUNT(*) AS nb_hotels,
       SUM(CASE WHEN h.hors_commune IS NOT NULL THEN 1 ELSE 0 END) AS communes_connues,
       SUM(CASE WHEN h.hors_commune THEN 1 ELSE 0 END) AS nb_hors_commune,
       ROUND(CAST(AVG(h.distance_km) AS NUMERIC), 2) AS distance_moyenne_km,
       ROUND(CAST(MAX(h.distance_km) AS NUMERIC), 2) AS distance_max_km
FROM hotels h
JOIN villes v ON h.id_ville = v.id_ville
GROUP BY v.id_ville, v.ville
ORDER BY nb_hors_commune DESC, v.ville ASC;
