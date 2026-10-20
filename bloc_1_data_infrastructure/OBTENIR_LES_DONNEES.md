# Données du projet Kayak

Toutes les données viennent de sources web publiques, interrogées par le code du dossier `kayak_v2/kayak/collecte/`. Le data lake de la collecte du 11/10/2026, sur lequel reposent les résultats publiés, n'est pas inclus dans ce dépôt.

| Donnée | Source | Licence ou conditions |
|---|---|---|
| Liste des destinations | page « 35 cities to visit in France » (one-week-in.com), lue avec BeautifulSoup après lecture de son `robots.txt` | contenu du site ; seule la liste réconciliée est gardée |
| Coordonnées des villes | API Nominatim | données © les contributeurs d'OpenStreetMap, ODbL ; une requête par seconde au plus |
| Météo à 7 jours | API Open-Meteo (ou OpenWeather si `OPENWEATHER_API_KEY` est fournie) | CC BY 4.0 ; API gratuite réservée à un usage non commercial |
| Hôtels | API Overpass (OpenStreetMap) | ODbL |

**Refaire une collecte.** Depuis `kayak_v2/`, après l'installation décrite dans le README :

```bash
python -m kayak.pipeline --etapes destinations geocodage meteo osm etl cartes
```

Sans l'option `--date`, la collecte est rangée sous la date du jour (`datalake/raw/<source>/date=AAAA-MM-JJ/`) ; avec `--date AAAA-MM-JJ`, les étapes `etl` et `cartes` se rejouent ensuite sur une collecte déjà archivée, sans rien collecter.

Les étapes sont nommées pour **exclure Booking.com** : le 10/10/2026, le site a opposé un contrôle anti-robot, que le projet respecte. Sans l'option `--etapes`, le pipeline tente aussi Booking.com.

Les moyens de contact des établissements que renvoie OpenStreetMap (courriels, téléphones) sont retirés de chaque réponse dès sa réception, avant l'archivage (`kayak_v2/kayak/collecte/minimisation.py`).

Une nouvelle collecte ne reproduit pas les chiffres publiés : les prévisions météo sont révisées d'un jour à l'autre et les données OpenStreetMap évoluent. Les résultats publiés viennent de la collecte du 11/10/2026.
