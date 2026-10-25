# Données : comment les obtenir

Aucun fichier de données n'est publié ici. Les jeux Walmart et Conversion ont été fournis avec l'énoncé de chaque projet ; ils ne sont pas redistribués. Les données Uber sont publiques, mais leur source n'affiche pas de licence : elles ne sont pas copiées non plus. Les tableaux qui affichaient des lignes individuelles de ces jeux ont été retirés des sorties publiées des notebooks (voir le README du bloc, section « Données »).

## Walmart (`1_Walmart/`)

- Fichier attendu à côté du notebook : `Walmart_Store_sales.csv` (8 749 octets, 150 lignes de données, 8 colonnes : `Store`, `Date`, `Weekly_Sales`, `Holiday_Flag`, `Temperature`, `Fuel_Price`, `CPI`, `Unemployment`).
- Origine : fourni avec l'énoncé du projet.
- SHA-256 : `51c782d28f899752cc30e34270ab5294af134af90d37d785701a0247e1c9837e`
- Sans ce fichier, le notebook s'arrête avec le message « Placez Walmart_Store_sales.csv à côté du notebook. »

## Conversion (`2_Conversion/`)

- Fichiers attendus à côté du notebook : `conversion_data_train.csv` (284 580 lignes, colonnes `country`, `age`, `new_user`, `source`, `total_pages_visited`, `converted`) et `conversion_data_test.csv` (31 620 lignes, mêmes colonnes sans `converted`).
- Origine : fournis avec l'énoncé du challenge.
- SHA-256 : `081a12c386cf92b5c322d7c22ce31d23bb214cfc0fefa8d93f36e620149fee41` (train) ; `d347f1320b97a03806992511875c177efc0bbad98f652928e802d5f9aa01f0c7` (test).

## Uber (`3_Uber/`)

Voir [`3_Uber/LIRE_DONNEES.md`](3_Uber/LIRE_DONNEES.md) : source publique (dépôt GitHub `fivethirtyeight/uber-tlc-foil-response`, dossier `uber-trip-data`), empreintes SHA-256 des six fichiers d'avril à septembre 2014, et trois façons de les fournir au notebook (dont la variable `UBER_DATA_ZIP`).

## Vérifier un fichier

```bash
python -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" Walmart_Store_sales.csv
```

Le fichier [`.gitignore`](.gitignore) de ce dossier empêche de versionner ces fichiers une fois placés pour reproduire les calculs.

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
