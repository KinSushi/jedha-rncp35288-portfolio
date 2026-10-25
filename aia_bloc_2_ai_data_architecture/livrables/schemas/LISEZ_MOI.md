# Diagrammes du dossier en images

Les 17 diagrammes des documents `00` à `13` publiés ici (le document 12, script de la vidéo, ne l'est pas) sont écrits en Mermaid, à côté du texte qui les explique. Ce dossier en donne une image PNG chacun, pour l'impression et la projection. Le texte Mermaid du document reste la source : une image se régénère à partir de lui.

Accessibilité : la **description textuelle** de chaque diagramme figure dans le document source, juste après le diagramme (section indiquée ci-dessous).

Le **plan du pipeline** est formé des images `05-1` (les deux voies, batch et temps réel), `05-2` (DAG quotidien) et `05-3` (démonstration autonome) ; le **schéma physique** est l'image `01-2`.

| Image | Document source | Section | Remarque |
|---|---|---|---|
| `01-1_architecture_logique.png` | `01_architecture_globale.md` | 2. Architecture logique | rendu vertical ; le document est horizontal |
| `01-2_architecture_physique.png` | `01_architecture_globale.md` | 3. Architecture physique (production) |  |
| `02-1_diagramme_entite_relation.png` | `02_oltp_erd.md` | 1. Diagramme entité-relation |  |
| `02-2_proprietes_acid_ce_qui_les.png` | `02_oltp_erd.md` | 3. Propriétés ACID : ce qui les garantit |  |
| `02-3_replication_et_haute_disponibilite.png` | `02_oltp_erd.md` | 4. Réplication et haute disponibilité (indicateur 6.2) |  |
| `03-1_schema_en_etoile.png` | `03_olap_schema.md` | 1. Schéma en étoile |  |
| `03-2_chargement.png` | `03_olap_schema.md` | 7. Chargement | rendu vertical ; le document est horizontal |
| `04-1_quatre_collections.png` | `04_nosql_modele.md` | 1. Quatre collections |  |
| `05-1_deux_voies_complementaires.png` | `05_pipeline.md` | 1. Deux voies complémentaires |  |
| `05-2_voie_batch_dag_airflow_quotidien.png` | `05_pipeline.md` | 2. Voie batch : DAG Airflow quotidien | rendu vertical ; le document est horizontal |
| `05-3_la_demonstration_autonome.png` | `05_pipeline.md` | 5. La démonstration autonome |  |
| `06-1_perimetre_et_zones_de_confiance.png` | `06_securite_conformite.md` | 1. Périmètre et zones de confiance | rendu vertical ; le document est horizontal |
| `07-1_chaine_de_bout_en_bout.png` | `07_integration_ml.md` | 2. Chaîne de bout en bout | rendu vertical ; le document est horizontal |
| `10-1_pannes_effet_et_parade.png` | `10_continuite_supervision_maintenance.md` | 3. Pannes : effet et parade (indicateur 6.3) |  |
| `10-2_chaine_d_alerte.png` | `10_continuite_supervision_maintenance.md` | 4.1 Chaîne d'alerte | rendu vertical ; le document est horizontal |
| `11-1_carte_des_interfaces.png` | `11_interfaces_responsabilites.md` | 1. Carte des interfaces | rendu vertical ; le document est horizontal |
| `11-2_processus_de_changement_d_une.png` | `11_interfaces_responsabilites.md` | 4. Processus de changement d'une interface | rendu vertical ; le document est horizontal |

Rendu : Mermaid 11.17.2 dans le navigateur Edge sans interface, le 17/10/2026 (titres de cadres entiers, marge sous chaque titre). Images `01-2` et `06-1` refaites le 19/10/2026 de la même façon, après l'ajout des modules `calcul` et `supervision` (8 clés KMS, 6 rôles IAM, tâches ECS Fargate, alarmes vers SNS). Image `01-2` refaite une seconde fois le 19/10/2026 : les points de terminaison d'interface ECR (API et registre Docker) du module `reseau` y figurent.

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
