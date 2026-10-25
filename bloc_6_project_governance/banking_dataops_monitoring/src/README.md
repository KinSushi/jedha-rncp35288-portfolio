# `src/` : code source

Deux choses cohabitent ici, volontairement.

## `banking_dataops/` : l'implémentation

Le package réel. Tout le code vit ici : un package donne un espace de noms, une
importabilité et une testabilité qu'un dossier de scripts épars ne donne pas.

```
banking_dataops/
├── config.py                  Settings pydantic, préfixe BANKING_DATAOPS_, DSN
├── db.py                      connexions, exécution SQL, découpage d'instructions
├── audit.py                   RunJournal, invariants, journalisation contrainte
├── logging_config.py          format humain ou JSON, corrélation par run_id
├── generate_synthetic_data.py couche bronze, déterministe par seed
├── ingest.py                  bronze → silver (COPY), application du schéma
├── quality_checks.py          les 7 contrôles et le verdict de publication
├── reconciliation.py          rapprochements inter-couches
├── monitoring.py              indicateurs d'exploitation
├── incident_report.py         rapports d'incident sur contrôle en échec
├── demo_scenario.py           injection / reset des anomalies de démonstration
├── powerbi_views.py           application du schéma en étoile
├── api.py                     API FastAPI en lecture seule
├── pipeline/
│   ├── cleaning.py            silver : répare la forme, jamais le fond
│   └── gold.py                schéma en étoile, truncate-then-insert
└── ml/
    ├── features.py            13 variables dérivées
    ├── train.py               référence linéaire vs forêt tunée, avec marge
    └── predict.py             scoring, priorisation des analystes
```

## `cleaning/`, `ingestion/`, `ml/`, `transformations/` : alias de chemins

Ces dossiers donnent à chaque étape un chemin d'entrée à plat : `src/ingestion/ingest.py`,
`src/cleaning/clean.py`, `src/transformations/`, `src/ml/predict.py`.

Ces modules ré-exportent depuis `banking_dataops`. Ils ne dupliquent rien :
une seconde copie de la logique divergerait en une semaine.

Les deux contraintes sont ainsi tenues : les chemins à plat se
résolvent, et le code reste un package.

Ne pas supprimer ces dossiers. Ils ne sont pas du code mort.

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
