# Données du projet Steam

Le fichier de données n'est pas versionné dans ce dépôt : le déposer dans ce dossier avant
d'exécuter le notebook, puis vérifier son empreinte.

- **Fichier attendu** : `steam_game_output.json` (63 688 273 octets, 55 691 enregistrements).
- **Source** : jeu de données de l'étude de cas, non redistribué ici ; il s'identifie par
  son nom, sa taille et son empreinte ci-dessous.
- **Empreinte SHA-256** : `7a8b65ce162d6297a4c79cae8a35481a45476b2f1a85d8f288634909c4569821` (téléchargé le 10/10/2026). Vérification : `sha256sum steam_game_output.json`
  (PowerShell : `Get-FileHash -Algorithm SHA256 steam_game_output.json`).
- **Utilisation** : en local, le notebook lit `donnees/steam_game_output.json` ; sur Databricks, il lit l'adresse du fichier donnée par la variable d'environnement `STEAM_CHEMIN_S3`, à définir avant l'exécution (`steam_analyse.py`, fonction `chemin_donnees`).

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
