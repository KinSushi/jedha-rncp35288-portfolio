# Note de preuve — Bloc 1 — Construction et alimentation d'une infrastructure de gestion de données

## Statut

Publié : le pipeline Kayak complet, présenté dans le [README du bloc](../README.md).

## Ce que le bloc démontre

Collecte web conforme au RGPD (BeautifulSoup, Scrapy, API publiques), stockage dans un data lake, ETL distribué avec Spark, chargement dans un entrepôt SQL, contrôle de la qualité des données extraites et cartographie des résultats.

## Preuves

| Preuve | Chemin |
|---|---|
| Notebook exécuté du pipeline | [`kayak_v2/Kayak_Bloc1_CDSD.ipynb`](../kayak_v2/Kayak_Bloc1_CDSD.ipynb) |
| Paquet du pipeline (collecte, stockage, ETL, cartes) | [`kayak_v2/kayak/`](../kayak_v2/kayak/) |
| Tests | [`kayak_v2/tests/`](../kayak_v2/tests/) |
| Requêtes d'exemple sur l'entrepôt | [`kayak_v2/entrepot/requetes_exemple.sql`](../kayak_v2/entrepot/requetes_exemple.sql) |
| Cartes produites | [`kayak_v2/sorties/`](../kayak_v2/sorties/) et [`docs/figures/`](figures/) |
| Obtention des données | [`OBTENIR_LES_DONNEES.md`](../OBTENIR_LES_DONNEES.md) |
