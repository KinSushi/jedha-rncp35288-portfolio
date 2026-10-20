# Copyright © 2026 · voir le fichier LICENSE du dépôt
#
# Environnement « prod » : la production. Même architecture et mêmes modules que `dev` ; seuls
# changent les tailles, la haute disponibilité (plusieurs instances dans des zones différentes),
# la protection contre la suppression et les durées de conservation.
#
# Ce qui change par rapport à `dev` (et SEULEMENT cela) est dans le bloc `locals` ci-dessous ;
# les modules et leurs garanties de sécurité (chiffrement, réseau privé, TLS, journaux) sont
# identiques. Toutes les tailles sont des HYPOTHÈSES de dimensionnement, expliquées dans
# livrables/aia2/09_dimensionnement_finops_greenops.md.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0.0, < 7.0.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6.0, < 4.0.0"
    }
  }

  # Configuration PARTIELLE : le bucket d'état, la clé et le verrou sont fournis à l'initialisation
  # (`terraform init -backend-config=backend.hcl`), pas écrits ici. Voir terraform/README.md.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.etiquettes
  }
}

locals {
  environnement = "prod"
  prefixe       = "${var.projet}-${local.environnement}"

  etiquettes = {
    projet        = var.projet
    environnement = local.environnement
    gere_par      = "terraform"
    donnees       = "synthetiques"
  }

  # --- Différences propres à l'environnement (HYPOTHÈSES de dimensionnement) ---------------
  aurora_classe              = "db.r6g.large"
  aurora_instances           = 2 # 1 écriture + 1 lecteur dans une autre zone = basculement possible
  documentdb_classe          = "db.r6g.large"
  documentdb_instances       = 3 # 1 écriture + 2 réplicas dans des zones différentes
  redshift_capacite_base_rpu = 32
  redshift_capacite_max_rpu  = 128
  redshift_plafond_rpu_h     = 5000 # plafond mensuel, à fixer avec la direction financière
  protection_suppression     = true
  autoriser_destruction      = false
  fenetre_secrets_jours      = 30
  sauvegardes_jours          = 35

  # Calcul (HYPOTHÈSE H16, document 09 § 4.5) : tailles Fargate et bornes de l'autoscaling du
  # service de score.
  taches = {
    score = { cpu = 1024, memoire = 2048 }
    etl   = { cpu = 2048, memoire = 8192 }
  }
  score_taches_min         = 2
  score_taches_max         = 6
  score_cible_cpu_pourcent = 60
  delai_montee_s           = 60
  delai_descente_s         = 300

  # Supervision (HYPOTHÈSE H17, document 09 § 2) : une heure de journal (WAL) à la pointe en
  # S2, soit 0,60 Gio par jour ÷ 24 × 5 (facteur de pointe H2) = 0,125 Gio = 128 Mio.
  seuil_retard_emplacement_octets = 134217728
}

# Garde-fou d'alerte : si la protection contre la suppression était levée par mégarde en
# production (par exemple en recopiant les réglages de `dev`), `terraform plan` afficherait un
# avertissement. Un bloc `check` ne bloque ni `plan` ni `apply` : il faut lire cet avertissement
# avant d'appliquer le plan.
check "protections_de_production" {
  assert {
    condition     = local.protection_suppression && !local.autoriser_destruction
    error_message = "En production, protection_suppression doit être vraie et autoriser_destruction fausse."
  }
}

# ------------------------------------------------------------------ chiffrement

module "kms" {
  source = "../../modules/kms"

  prefixe = local.prefixe
  region  = var.region

  cles = {
    oltp       = { description = "Base transactionnelle Aurora" }
    documents  = { description = "Base de documents DocumentDB" }
    analytique = { description = "Entrepot Redshift Serverless" }
    lac        = { description = "Lac de donnees S3" }
    secrets    = { description = "Secrets Manager et mots de passe geres par les services" }
    sauvegarde = { description = "Coffre AWS Backup (sauvegardes des bases)" }
    journaux   = { description = "Journaux CloudWatch et archive d'audit", journaux_cloudwatch = true }
    alertes    = { description = "Rubrique SNS des alertes de supervision", alertes_sns = true }
  }

  fenetre_suppression_jours = 30
}

# ------------------------------------------------------------------ réseau privé

module "reseau" {
  source = "../../modules/reseau"

  prefixe          = local.prefixe
  region           = var.region
  cidr             = var.cidr_vpc
  zones            = var.zones
  cle_journaux_arn = module.kms.arns["journaux"]
}

# ------------------------------------------------------------------ stockage objet

module "lac" {
  source = "../../modules/lac_s3"

  prefixe               = local.prefixe
  cle_lac_arn           = module.kms.arns["lac"]
  cle_audit_arn         = module.kms.arns["journaux"]
  autoriser_destruction = local.autoriser_destruction
}

# ------------------------------------------------------------------ secrets

module "secrets" {
  source = "../../modules/secrets"

  prefixe                    = local.prefixe
  cle_kms_arn                = module.kms.arns["secrets"]
  fenetre_recuperation_jours = local.fenetre_secrets_jours
  fonction_rotation_arn      = var.fonction_rotation_arn
}

# ------------------------------------------------------------------ bases de données

module "aurora" {
  source = "../../modules/aurora_postgresql"

  prefixe                     = local.prefixe
  sous_reseaux_ids            = module.reseau.sous_reseaux_prives
  zones                       = module.reseau.zones
  groupe_securite_id          = module.reseau.groupes_securite["base_transactionnelle"]
  cle_kms_arn                 = module.kms.arns["oltp"]
  cle_secrets_arn             = module.kms.arns["secrets"]
  cle_journaux_arn            = module.kms.arns["journaux"]
  classe_instance             = local.aurora_classe
  nb_instances                = local.aurora_instances
  retention_sauvegardes_jours = local.sauvegardes_jours
  protection_suppression      = local.protection_suppression
}

module "documentdb" {
  source = "../../modules/documentdb"

  prefixe                     = local.prefixe
  sous_reseaux_ids            = module.reseau.sous_reseaux_prives
  zones                       = module.reseau.zones
  groupe_securite_id          = module.reseau.groupes_securite["documents"]
  cle_kms_arn                 = module.kms.arns["documents"]
  cle_journaux_arn            = module.kms.arns["journaux"]
  classe_instance             = local.documentdb_classe
  nb_instances                = local.documentdb_instances
  retention_sauvegardes_jours = local.sauvegardes_jours
  protection_suppression      = local.protection_suppression
}

# ------------------------------------------------------------------ sauvegardes centralisées

module "sauvegarde" {
  source = "../../modules/sauvegarde"

  prefixe           = local.prefixe
  cle_kms_arn       = module.kms.arns["sauvegarde"]
  cles_sources_arns = [module.kms.arns["oltp"], module.kms.arns["documents"]]
  ressources_arns   = [module.aurora.cluster_arn, module.documentdb.cluster_arn]
  retention_jours   = local.sauvegardes_jours
}

# ------------------------------------------------------------------ bus de messages

module "msk" {
  source = "../../modules/msk_serverless"

  prefixe            = local.prefixe
  sous_reseaux_ids   = module.reseau.sous_reseaux_prives
  groupe_securite_id = module.reseau.groupes_securite["bus"]
}

# ------------------------------------------------------------------ identités et droits

module "iam" {
  source = "../../modules/iam"

  prefixe                    = local.prefixe
  region                     = var.region
  lac_arn                    = module.lac.lac_arn
  cle_lac_arn                = module.kms.arns["lac"]
  cle_secrets_arn            = module.kms.arns["secrets"]
  secrets_traitement_arns    = [module.secrets.arn_sel_pseudonymisation]
  secret_cdc_arn             = module.secrets.arn_cdc
  msk_cluster_arn            = module.msk.cluster_arn
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  redshift_workgroup_arn     = module.redshift.workgroup_arn
  principaux_analystes       = var.principaux_analystes
}

# ------------------------------------------------------------------ entrepôt analytique

module "redshift" {
  source = "../../modules/redshift_serverless"

  prefixe                    = local.prefixe
  sous_reseaux_ids           = module.reseau.sous_reseaux_prives
  groupe_securite_id         = module.reseau.groupes_securite["entrepot"]
  cle_kms_arn                = module.kms.arns["analytique"]
  cle_secrets_arn            = module.kms.arns["secrets"]
  role_s3_arn                = module.iam.role_redshift_s3_arn
  capacite_base_rpu          = local.redshift_capacite_base_rpu
  capacite_max_rpu           = local.redshift_capacite_max_rpu
  plafond_rpu_heures_mensuel = local.redshift_plafond_rpu_h
}

# ------------------------------------------------------------------ calcul (ECS sur Fargate)

module "calcul" {
  source = "../../modules/calcul"

  prefixe            = local.prefixe
  region             = var.region
  sous_reseaux_ids   = module.reseau.sous_reseaux_prives
  groupe_securite_id = module.reseau.groupes_securite["application"]
  cle_journaux_arn   = module.kms.arns["journaux"]
  depots_ecr_arns    = var.depots_ecr_arns
  image_score        = var.image_score
  image_etl          = var.image_etl
  role_score_arn     = module.iam.role_score_arn
  role_etl_arn       = module.iam.role_etl_arn

  # Adresses et noms seulement : aucun secret (lus dans Secrets Manager par le code).
  environnement_score = {
    KAFKA_SERVEURS       = module.msk.serveurs_amorce_iam
    DOCUMENTDB_HOTE      = module.documentdb.point_ecriture
    GROUPE_CONSOMMATEURS = "score-fraude"
  }
  environnement_etl = {
    LAC_BUCKET         = module.lac.lac_id
    AURORA_LECTURE     = module.aurora.point_lecture
    REDSHIFT_WORKGROUP = module.redshift.workgroup_nom
  }

  taches                   = local.taches
  score_taches_min         = local.score_taches_min
  score_taches_max         = local.score_taches_max
  score_cible_cpu_pourcent = local.score_cible_cpu_pourcent
  delai_montee_s           = local.delai_montee_s
  delai_descente_s         = local.delai_descente_s
}

# ------------------------------------------------------------------ supervision

module "supervision" {
  source = "../../modules/supervision"

  prefixe                         = local.prefixe
  cle_alertes_arn                 = module.kms.arns["alertes"]
  adresses_astreinte              = var.adresses_astreinte
  aurora_cluster_identifiant      = module.aurora.cluster_identifiant
  seuil_retard_emplacement_octets = local.seuil_retard_emplacement_octets
  redshift_workgroup_nom          = module.redshift.workgroup_nom
  plafond_rpu_heures_mensuel      = local.redshift_plafond_rpu_h
  cluster_ecs_nom                 = module.calcul.cluster_nom
  service_score_nom               = module.calcul.service_score_nom
}
