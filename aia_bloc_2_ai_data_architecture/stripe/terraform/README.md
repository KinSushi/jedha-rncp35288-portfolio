# Infrastructure AWS (Terraform)

Déclare l'architecture physique du document `livrables/aia2/01_architecture_globale.md` : réseau privé, Aurora PostgreSQL, lac S3, MSK Serverless, DocumentDB, Redshift Serverless, secrets, IAM au moindre privilège, sauvegardes, tâches de calcul (ECS sur Fargate, autoscaling du service de score) et supervision (alarmes CloudWatch, rubrique SNS).

> **État de vérification, à lire avant toute chose.** Ce code a été analysé par `checkov` (536 contrôles réussis, 0 échec, 18 ignorés avec justification, le 19/10/2026 : `../../livrables/aia2/checkov.txt`) et par des tests statiques (`pytest tests/test_terraform.py`). **`terraform init`, `validate`, `plan` et `fmt` n'ont PAS été exécutés** (binaire non installé, aucun compte AWS). Les arguments des ressources ont été comparés à la documentation du fournisseur, mais seule l'exécution de `terraform validate` le prouve. Rien n'a été déployé.

## Structure

```text
terraform/
├── modules/
│   ├── kms/                  une clé KMS par domaine de données, rotation annuelle
│   ├── reseau/               VPC privé (sans Internet ni NAT), groupes de sécurité, points de terminaison, journaux de flux
│   ├── bucket_s3/            bucket durci (chiffrement, versionnement, TLS, cycle de vie), réutilisé 3 fois
│   ├── lac_s3/               lac, journaux d'accès, archive d'audit verrouillée
│   ├── aurora_postgresql/    OLTP, multi-zones, chiffré, mot de passe géré
│   ├── documentdb/           base de documents
│   ├── msk_serverless/       bus de messages
│   ├── redshift_serverless/  entrepôt analytique, plafond de consommation
│   ├── secrets/              sel de pseudonymisation, compte Debezium
│   ├── iam/                  six rôles sans joker
│   ├── sauvegarde/           AWS Backup
│   ├── calcul/               ECS sur Fargate : service de score (autoscaling), tâche ETL, rôle d'exécution
│   └── supervision/          alarmes CloudWatch, événements Aurora, rubrique SNS chiffrée
└── environnements/
    ├── dev/                  petit, destructible
    └── prod/                 haute disponibilité, protections, rotation des secrets obligatoire
```

Chaque module a `main.tf`, `variables.tf`, `outputs.tf`, `versions.tf`. Les variables sont typées, validées et décrites ; les tailles sont des **hypothèses** (document 09, registre H1 à H17).

## Prérequis

- Terraform 1.6 ou plus ; fournisseur `hashicorp/aws` 6.x et `hashicorp/random` 3.x (verrouillés par `terraform init` dans `.terraform.lock.hcl`, à versionner).
- Un compte AWS et des identifiants fournis **par l'environnement** (profil, rôle) : jamais dans le code.
- Un bucket d'état et une table de verrou, créés une fois (voir ci-dessous).

## Utilisation

```text
cd environnements/dev
copy backend.hcl.exemple backend.hcl        # Linux ou macOS : cp ; puis renseigner les valeurs
copy terraform.tfvars.exemple terraform.tfvars   # facultatif
terraform init -backend-config=backend.hcl
terraform fmt -check -recursive ../..
terraform validate
terraform plan -out=plan.tfplan
terraform apply plan.tfplan                 # après relecture du plan
```

Sans compte AWS, `terraform init -backend=false` puis `terraform validate` contrôlent la syntaxe et la cohérence des modules (le fournisseur est téléchargé ; aucun appel AWS). `terraform plan` exige des identifiants.

En production : `cd environnements/prod` ; la variable `fonction_rotation_arn` (fonction de rotation du secret Debezium) est **obligatoire**. Dans les deux environnements, les images du calcul sont obligatoires (`depots_ecr_arns`, `image_score`, `image_etl`, désignées par leur empreinte `@sha256:`) ; `adresses_astreinte` est facultative.

## Reproductibilité et idempotence

- **Déclaratif** : le code décrit l'état voulu ; `terraform apply` sur un état déjà conforme ne modifie rien (`terraform plan` doit alors annoncer « No changes »). Ce comportement est celui de Terraform ; il n'a pas pu être constaté ici faute de `plan` exécuté.
- **Noms déterministes** : tous les noms dérivent de `projet` et `environnement` (`stripe-dev-…`), aucune valeur tirée au hasard dans un nom ; les seules valeurs aléatoires sont les secrets (`random_password`), stables tant qu'elles restent dans l'état.
- **Versions figées** : plages de versions du fournisseur dans chaque `versions.tf`, verrou `.terraform.lock.hcl` à versionner, versions de moteur en variables.
- **Mêmes modules pour `dev` et `prod`** : seules les valeurs changent (test `test_dev_et_prod_ont_la_meme_structure`).
- **Garde-fous** : validations de variables, bloc `check` en production, protection contre la suppression des bases.

## Modularité et versioning

Chaque module est autonome (variables d'entrée typées, sorties, versions de fournisseur) et réutilisable ; `bucket_s3` est utilisé trois fois par `lac_s3`. Le code est versionné dans Git : les changements d'infrastructure passent par une branche, une revue et un `plan` joint. Pour réutiliser un module depuis un autre dépôt, le référencer par étiquette :

```hcl
module "kms" {
  source = "git::https://exemple.invalid/organisation/depot.git//stripe/terraform/modules/kms?ref=v1.0.0"
  # …
}
```

(adresse fictive ; poser une étiquette de version sur le dépôt avant de s'y référer).

## Analyse de sécurité

```text
pip install checkov
checkov -d stripe/terraform --framework terraform --compact
```

Résultat joint : `../../livrables/aia2/checkov.txt`. Les 18 contrôles ignorés (8 règles) le sont par des commentaires `#checkov:skip=… : justification` placés sur la ressource ; chaque justification est reprise dans `06_securite_conformite.md` § 9.

## Bucket d'état et verrou (à créer une fois, commandes indicatives non exécutées)

```text
aws s3api create-bucket --bucket NOM-DU-BUCKET-D-ETAT --region eu-west-1 --create-bucket-configuration LocationConstraint=eu-west-1
aws s3api put-bucket-versioning --bucket NOM-DU-BUCKET-D-ETAT --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket NOM-DU-BUCKET-D-ETAT --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket NOM-DU-BUCKET-D-ETAT --server-side-encryption-configuration "{\"Rules\":[{\"ApplyServerSideEncryptionByDefault\":{\"SSEAlgorithm\":\"aws:kms\",\"KMSMasterKeyID\":\"alias/NOM-DE-LA-CLE-D-ETAT\"}}]}"
aws dynamodb create-table --table-name NOM-DE-LA-TABLE-DE-VERROU --attribute-definitions AttributeName=LockID,AttributeType=S --key-schema AttributeName=LockID,KeyType=HASH --billing-mode PAY_PER_REQUEST
```

L'état contient des valeurs sensibles (mots de passe aléatoires du module `secrets`) : accès restreint, chiffrement et versionnement sont indispensables.

## Principes de sécurité appliqués

- Réseau privé : aucune passerelle Internet ni NAT ; accès aux services AWS par points de terminaison privés.
- Chiffrement au repos avec des clés du client (une par domaine) ; TLS partout.
- Aucun mot de passe dans le code : les services créent et renouvellent les leurs.
- Droits IAM sans joker ; protection contre la suppression en production, vérifiée par un bloc `check`.
- Journaux conservés 365 jours.

## Limites connues

- Les valeurs par défaut de version de moteur (`16.4` pour Aurora, `5.0.0` pour DocumentDB) sont des hypothèses à confirmer dans la région.
- Les noms de sujets et de groupes internes de MSK Connect (politique du rôle `cdc`) sont à confirmer lors du premier déploiement.
- Debezium sur MSK Connect, les tâches de calcul et les alarmes de supervision ne sont pas déclarés ici.
- Pas de réplication inter-régions (choix documenté).

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
