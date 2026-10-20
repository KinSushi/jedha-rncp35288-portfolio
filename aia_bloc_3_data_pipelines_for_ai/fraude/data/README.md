# Dossier `data/`

Ce dossier reçoit le jeu `fraudTest.csv` (environ 150 Mo, transactions synthétiques). **Il n'est
jamais versionné** : le fichier est exclu par le `.gitignore` du dépôt, et il n'est pas
redistribué ici.

## Obtenir le jeu

Le jeu est fourni avec l'énoncé du projet. Deux façons de le mettre en place :

1. **Fichier déjà en main** : le copier dans ce dossier sous le nom `fraudTest.csv`, ou indiquer
   son chemin par la variable `FRAUDE_JEU_CHEMIN`.
2. **Téléchargement** : renseigner l'adresse fournie avec l'énoncé dans `config/base.yaml`
   (clé `source.jeu_url`), puis lancer, depuis `fraude/` :

   ```bash
   python -m detection_fraude.telechargement
   ```

   La commande vérifie la taille (150 354 339 octets) et l'empreinte SHA-256 inscrites dans
   `config/base.yaml` ; un fichier qui ne correspond pas est refusé.

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
