# Données : où les obtenir

Ce dépôt ne contient **aucun jeu de données d'entrée**. Les jeux fournis avec les énoncés des études de cas ne sont pas redistribués ; les jeux publics ne sont pas copiés non plus. Chaque bloc explique comment obtenir ses données, où les déposer et comment vérifier qu'il s'agit bien du fichier qui a produit les résultats publiés (taille et empreinte SHA-256).

Le fichier [`.gitignore`](.gitignore) de la racine, complété par celui de chaque bloc, empêche de versionner ces fichiers une fois déposés pour reproduire les calculs.

## Tableau par bloc

| Bloc | Nature des données | Comment les obtenir |
|---|---|---|
| [Bloc 1 : infrastructure de données](bloc_1_data_infrastructure/) | Données web publiques, collectées par le code du projet (liste de destinations, géocodage, météo, hôtels) | [`OBTENIR_LES_DONNEES.md`](bloc_1_data_infrastructure/OBTENIR_LES_DONNEES.md) : relancer la collecte avec le pipeline du projet |
| [Bloc 2 : analyse exploratoire et statistique](bloc_2_eda_statistics/) | Deux jeux fournis avec l'énoncé des études de cas | [`README.md`](bloc_2_eda_statistics/README.md), section « Données », et [`2_Steam/donnees/LIRE_DONNEES.md`](bloc_2_eda_statistics/2_Steam/donnees/LIRE_DONNEES.md) |
| [Bloc 3 : apprentissage automatique](bloc_3_machine_learning/) | Deux jeux fournis avec l'énoncé ; un jeu public pour le troisième projet | [`DONNEES.md`](bloc_3_machine_learning/DONNEES.md) et [`3_Uber/LIRE_DONNEES.md`](bloc_3_machine_learning/3_Uber/LIRE_DONNEES.md) |
| [Bloc 4 : données non structurées](bloc_4_nlp_deep_learning/) | Corpus de SMS fourni avec l'énoncé | [`README.md`](bloc_4_nlp_deep_learning/README.md), section « Données » |
| Bloc 5 : industrialisation | Non publié dans cette version | sans objet |
| [Bloc 6 : direction de projets de données](bloc_6_project_governance/) | Données entièrement synthétiques, générées par le projet | [`README.md`](bloc_6_project_governance/README.md), section « Données », et [`data/README.md`](bloc_6_project_governance/banking_dataops_monitoring/data/README.md) |
| [AIA bloc 1 : gouvernance des données et de l'IA](aia_bloc_1_data_ai_governance/) | Aucun jeu de données ; une étude de cas fournie avec l'énoncé | [`OBTENIR_LES_DONNEES.md`](aia_bloc_1_data_ai_governance/OBTENIR_LES_DONNEES.md) |
| [AIA bloc 2 : architecture de données pour l'IA](aia_bloc_2_ai_data_architecture/) | Données entièrement synthétiques, générées par la démonstration | [`README.md`](aia_bloc_2_ai_data_architecture/README.md), section « Données » |
| [AIA bloc 3 : pipelines de données pour l'IA](aia_bloc_3_data_pipelines_for_ai/) | Jeu de transactions synthétiques et API de démonstration fournis avec l'énoncé | [`README.md`](aia_bloc_3_data_pipelines_for_ai/README.md), section « Données », et [`fraude/data/README.md`](aia_bloc_3_data_pipelines_for_ai/fraude/data/README.md) |
| AIA bloc 4 : déploiement de solutions d'IA | Non publié dans cette version | sans objet |

## Règles communes

- Déposer chaque fichier à l'emplacement indiqué par le bloc ; ne jamais l'ajouter à Git.
- Vérifier l'empreinte avant d'exécuter un notebook : `sha256sum <fichier>` (sous PowerShell : `Get-FileHash -Algorithm SHA256 <fichier>`).
- Sans les données, le code et les sorties enregistrées des notebooks restent lisibles ; chaque bloc précise dans son README quels tests ont besoin des données.
- Ne jamais placer dans ce dépôt de données réelles de clients, de patients, d'employeurs ou de personnes.

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
