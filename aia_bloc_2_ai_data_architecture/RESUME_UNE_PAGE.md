# Résumé en une page : architecture de données de type Stripe

**Contexte.** Une plateforme de paiement en ligne relie des marchands, leurs clients et des moyens de paiement. Elle doit enregistrer chaque transaction sans la perdre ni la dupliquer, analyser de gros volumes, ranger des données peu structurées et noter chaque paiement avec un modèle de fraude, sous PCI-DSS v4.0.1, RGPD et CCPA. Les données sont **synthétiques** (document 00 § 1).

**Enjeux.** Intégrité financière (idempotence, ACID), fraîcheur des alertes de fraude, exactitude de l'analytique, conformité et souveraineté, maîtrise des coûts (documents 00 § 3 et § 4).

**Choix d'architecture** (documents 01 à 07) :

| Besoin | Choix | Pourquoi |
|---|---|---|
| Écrire les paiements | PostgreSQL (Aurora en production), 13 tables en troisième forme normale, clé d'idempotence unique | garanties ACID ; un paiement rejoué n'est pas débité deux fois |
| Relier les bases | capture des changements (Debezium) puis Kafka, 8 tables publiées | lire le journal de transactions au lieu d'interroger les tables |
| Analyser | entrepôt en étoile (Redshift Serverless ; DuckDB en démonstration) | lectures massives séparées des écritures |
| Données souples | base de documents, 4 collections, événements supprimés après 90 jours | structure souple, minimisation |
| Fraude | modèle d'arbres entraîné sur l'entrepôt, noté sur le flux, dérive suivie par le PSI | réentraînement si PSI > 0,2 (hypothèse H8) |
| Infrastructure | Terraform AWS : 13 modules (dont le calcul ECS Fargate avec autoscaling et la supervision : alarmes, SNS), environnements `dev` et `prod`, 8 clés KMS, 6 rôles IAM | réseau privé, chiffrement par domaine, moindre privilège |

**Preuves.** Démonstration exécutable (OLTP, CDC, étoile, documents, modèle, PSI) ; 774 tests réussis et 6 ignorés (ils exigent Docker) sur la version publiée ; analyse checkov du Terraform : 536 contrôles réussis, 0 échec, 18 ignorés avec justification écrite (19/10/2026, calcul et supervision compris ; 461 / 0 / 18 avant leur ajout) ; DDL rejoué sur PostgreSQL 16 ; 298,5 octets par paiement mesurés, index compris (documents 09 § 1 et 13 § 2).

**Coûts.** Chiffrage daté du 10/10/2026 (liste de prix publique AWS, eu-west-1) : au moins 2 471 USD par mois en production, au moins 824 USD en développement. Ce sont des **planchers** : plusieurs postes ne sont pas chiffrés (document 09 § 5.5).

**Limites dites** (document 13 § 3) : rien n'est déployé sur AWS ; Terraform n'a pas été exécuté (ni `init`, ni `validate`, ni `plan`) ; MongoDB, Redpanda et Debezium n'ont pas tourné ; le DDL Redshift n'a pas été exécuté ; le DAG Airflow est codé et testé sans Airflow, jamais exécuté par un ordonnanceur ; les tâches de calcul et les alarmes sont codées en Terraform, non déployées ; la fonction de rotation des secrets est décrite, non codée ; la vidéo du pipeline n'est pas produite. Indicateurs de la grille : 26 couverts sur 27, le 4.3 est partiel (document 13 § 1).

**Pour aller plus loin.** `README.md` (présentation, résultats et commandes), `livrables/aia2/` (13 documents ; le script de la vidéo n'est pas publié), `livrables/schemas/` (les 17 diagrammes publiés, en images), `stripe/` (code, tests et infrastructure).

---

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
