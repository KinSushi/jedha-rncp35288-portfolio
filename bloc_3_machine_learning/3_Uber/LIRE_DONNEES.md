# Uber Pickups : où sont les données

Le notebook `Uber_Pickups_Bloc3_CDSD_v2.ipynb` lit six fichiers CSV mensuels, d'avril à septembre 2014 : ce sont les seuls fichiers de la collection qui contiennent des coordonnées GPS (colonnes `Date/Time`, `Lat`, `Lon`, `Base`). Les données ne sont pas dupliquées dans ce dossier : elles pèsent environ 100 Mo.

## Source publique

- Dépôt GitHub : <https://github.com/fivethirtyeight/uber-tlc-foil-response> (FiveThirtyEight), dossier `uber-trip-data`. Le dépôt les décrit ainsi : données de courses Uber obtenues par une demande d'accès à l'information auprès de la Taxi & Limousine Commission de New York.
- Vérification faite le 11/10/2026 : pour les six fichiers de 2014, la taille et l'empreinte Git (SHA-1 de l'objet « blob ») listées par l'API publique du dépôt sont **identiques** à celles des fichiers utilisés ici. Les fichiers sont donc ceux du dépôt public, octet pour octet.
- La page du dépôt n'affiche pas de licence : vérifier les conditions de réutilisation avant toute rediffusion des données.

## Empreintes SHA-256 des fichiers de 2014

Le notebook recalcule ces empreintes à chaque exécution et affiche « Empreintes SHA-256 conformes : 6 sur 6 ».

| Fichier | Taille (octets) | SHA-256 |
|---|---|---|
| `uber-raw-data-apr14.csv` | 26 110 064 | `4c92b95b9918e73ae84bade0bffef6e758e792c247469523130d3c1ad7b646e0` |
| `uber-raw-data-may14.csv` | 30 174 084 | `3940ebee250998e32e810fed596fa2e5ac170e6926e0da43b977b6f76c6921e9` |
| `uber-raw-data-jun14.csv` | 30 695 600 | `ca999fb126a068a429c266d6781b1e8825b2acc1c74644481222ca6f01dbb557` |
| `uber-raw-data-jul14.csv` | 36 870 544 | `42bc47c906a3d17b2b4f9543594ab300db9ac5f8bbb7a6060d215fa30a8c62c2` |
| `uber-raw-data-aug14.csv` | 38 324 585 | `710e5766a8f2c13cfa9fd1dda30a4f6c5cba1b964a1efeb7087df0085a86f091` |
| `uber-raw-data-sep14.csv` | 47 540 750 | `3c7bda88de4106b5ebeb489545a17b09133a6e5c0efde71d9666d0ee14b7b7d9` |

**Archive utilisée pour les exécutions** : `uber-trip-data.zip` (101 042 186 octets), SHA-256
`ab3bb1fc4943b2577c1f5e990ea7cbeeb4c89cd0b823ac8de974359475a298a0`. Elle contient le dossier `uber-trip-data/` du dépôt (les six fichiers de 2014, le fichier 2015 compressé et `taxi-zone-lookup.csv`). Une archive reconstituée à partir du dépôt aura une empreinte différente (l'empreinte d'une archive dépend de sa fabrication) : ce sont les empreintes des six fichiers, ci-dessus, qui font foi.

## Pour réexécuter le notebook

Trois possibilités, par ordre de priorité (le notebook choisit seul) :

| Possibilité | Ce qu'il faut faire |
|---|---|
| Variable d'environnement | Définir `UBER_DATA_ZIP` avec le chemin de l'archive `uber-trip-data.zip`, où qu'elle soit |
| Dossier extrait | Placer le dossier `uber-trip-data/` du dépôt, avec ses six fichiers CSV, à côté du notebook |
| Archive à côté du notebook | Placer `uber-trip-data.zip` dans ce dossier |

Les fichiers sont lus **directement dans l'archive** : rien n'est extrait ni écrit sur le disque. Le notebook retrouve son dossier même si le noyau Jupyter démarre ailleurs (il remonte depuis le dossier courant jusqu'au dossier qui le contient).

Le fichier 2015 (`uber-raw-data-janjune-15.csv.zip`) n'est pas utilisé : il contient des identifiants de zones de taxi et pas de coordonnées GPS.

## Pour exécuter les tests

Les tests du module `uber_bloc3.py` (`tests/test_uber_bloc3.py`) n'ont besoin d'aucune donnée. Les tests qui exécutent le notebook sur les vraies données (`tests/test_notebook_uber.py`) cherchent l'archive de la même façon que le notebook : variable `UBER_DATA_ZIP`, puis `uber-trip-data.zip` à côté du notebook. Sans archive, ils sont **ignorés** (et pytest le dit) ; deux d'entre eux, qui fabriquent leur propre petite archive, tournent toujours.

```powershell
$env:UBER_DATA_ZIP = "C:\chemin\vers\uber-trip-data.zip"
python -m pytest tests -W error::DeprecationWarning -W error::PendingDeprecationWarning
```

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
