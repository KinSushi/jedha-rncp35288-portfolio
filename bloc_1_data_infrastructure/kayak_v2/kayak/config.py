# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Configuration centralisée du projet Kayak.

Tous les paramètres sensibles ou modulables sont lus depuis les variables
d'environnement ; les valeurs par défaut permettent une exécution entièrement
locale, sans infrastructure cloud.

CHOIX
-----
- **Retenu : des variables d'environnement**, avec une valeur par défaut locale pour chacune
  (``KAYAK_S3_BUCKET``, ``KAYAK_DB_URL``, ``OPENWEATHER_API_KEY``, ``KAYAK_USER_AGENT``,
  ``KAYAK_DATE_COLLECTE``). Les secrets (clé OpenWeather, identifiants AWS) ne sont donc jamais
  dans le code. *Écarté* : un fichier de configuration versionné, où un secret finirait par être
  écrit.
- **Retenu : la liste des 35 destinations de l'énoncé écrite ici**, dans l'ordre de l'énoncé, qui
  fixe les identifiants de ville de 1 à 35. *Écarté* : la déduire de la page source, dont les noms
  diffèrent (« Verdon Gorge », « Nimmes ») : la réconciliation est faite par
  ``kayak/collecte/destinations.py``.
- **Retenu : une « aire de destination » de 8 km** (``RAYON_AIRE_M``) plutôt que la commune : une
  destination est une zone (massif, département) et pas toujours une commune.
- **Retenu : la date de collecte par variable d'environnement** (``date_collecte``), pour rejouer
  une collecte historique de façon idempotente. *Écarté* : la date du jour seule, qui rendrait les
  tests et les rejeux dépendants de l'horloge.

OÙ
---
- **Place** : module lu par tous les autres ; il ne dépend d'aucun module du projet.
- **Appelants** : ``kayak/collecte`` (User-Agent, date, rayon, clé), ``kayak/etl/spark_etl.py``
  (destinations non communales), ``kayak/pipeline.py`` (URL de l'entrepôt),
  ``kayak/stockage/data_lake.py`` (bucket, préfixe, dossier local), ``kayak/cartes.py`` (dossier
  des sorties).
- **Dépendances** : bibliothèque standard seulement.

COMMENT
-------
- Les constantes sont lues UNE fois, à l'import ; ``date_collecte()`` relit sa variable à chaque
  appel. ``RACINE`` est le dossier du projet, déduit de l'emplacement du fichier.
- **Preuves** : valeurs par défaut (tests/test_config.py:92) ; la variable prime sur la date du
  jour (tests/test_config.py:108).
- **Refus** (tests inverses) : la liste des destinations non communales ne peut pas être modifiée
  (tests/test_config.py:173) ; lire la date laisse l'environnement inchangé
  (tests/test_config.py:197).

POURQUOI
--------
- Le rayon de 8 km et les trois destinations qui ne sont pas des communes (Gorges du Verdon,
  Ariège, château du Haut-Kœnigsbourg) sont des choix du projet, décrits dans le README, point 2
  des limites ; source externe : inconnue.
- La liste des 35 villes vient de l'énoncé du bloc 1 (non joint à ce dépôt) ;
  la liste exacte est contrôlée par tests/test_config.py:159.
- Le User-Agent par défaut identifie le projet et son cadre (RNCP35288), comme l'exige la
  politique d'usage de Nominatim (https://operations.osmfoundation.org/policies/nominatim/,
  lecture non consignée, à revérifier).

JOURNALISATION
--------------
- Aucune : ce module ne journalise rien et ne lève aucune exception. Il ne contient que des
  constantes et une fonction pure.
"""

import os
from datetime import date
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent

VILLES_ENONCE = [
    "Mont Saint Michel",
    "St Malo",
    "Bayeux",
    "Le Havre",
    "Rouen",
    "Paris",
    "Amiens",
    "Lille",
    "Strasbourg",
    "Chateau du Haut Koenigsbourg",
    "Colmar",
    "Eguisheim",
    "Besancon",
    "Dijon",
    "Annecy",
    "Grenoble",
    "Lyon",
    "Gorges du Verdon",
    "Bormes les Mimosas",
    "Cassis",
    "Marseille",
    "Aix en Provence",
    "Avignon",
    "Uzes",
    "Nimes",
    "Aigues Mortes",
    "Saintes Maries de la mer",
    "Collioure",
    "Carcassonne",
    "Ariege",
    "Toulouse",
    "Montauban",
    "Biarritz",
    "Bayonne",
    "La Rochelle",
]

USER_AGENT = os.environ.get(
    "KAYAK_USER_AGENT",
    "KayakEtudeCertif/1.0 (projet pedagogique RNCP35288; robot identifie)",
)

# « Aire de destination » : une destination n'est pas un point mais une zone. Les hôtels sont
# recherchés dans un rayon de 8 km autour du centre géocodé de la destination. Ce rayon dépasse
# parfois la commune (La Grande-Motte et Le Grau-du-Roi pour Aigues-Mortes, La Ciotat pour
# Cassis) : l'ETL mesure donc la distance de chaque hôtel au centre et marque ceux dont la
# commune OpenStreetMap (addr:city) diffère de celle de la destination.
RAYON_AIRE_M = 8000

# Destinations qui ne sont pas des communes (un massif, un département, un monument) : comparer
# la commune d'un hôtel à leur nom n'a aucun sens, le marquage « hors commune » reste alors vide.
DESTINATIONS_NON_COMMUNALES = (
    "Gorges du Verdon",
    "Ariege",
    "Chateau du Haut Koenigsbourg",
)

S3_BUCKET = os.environ.get("KAYAK_S3_BUCKET")
S3_PREFIXE = "kayak"

DATALAKE_LOCAL = RACINE / "datalake"

DB_URL = os.environ.get("KAYAK_DB_URL", "sqlite:///" + str(RACINE / "entrepot" / "kayak.db"))

OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY")


def date_collecte() -> str:
    """Renvoie la date de collecte, figée par variable d'environnement ou du jour.

    La variable KAYAK_DATE_COLLECTE permet de rejouer une collecte historique
    et de garantir l'idempotence des tests et du pipeline.
    """
    return os.environ.get("KAYAK_DATE_COLLECTE", date.today().isoformat())
