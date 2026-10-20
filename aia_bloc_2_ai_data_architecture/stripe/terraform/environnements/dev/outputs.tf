# Copyright © 2026 · voir le fichier LICENSE du dépôt
#
# Sorties : uniquement des identifiants et des adresses, jamais de secret.

output "aurora_point_ecriture" {
  description = "Point de terminaison d'écriture d'Aurora PostgreSQL."
  value       = module.aurora.point_ecriture
}

output "aurora_point_lecture" {
  description = "Point de terminaison de lecture d'Aurora PostgreSQL."
  value       = module.aurora.point_lecture
}

output "aurora_secret_administrateur_arn" {
  description = "ARN du secret (géré par Aurora) qui contient le mot de passe administrateur."
  value       = module.aurora.secret_administrateur_arn
}

output "documentdb_point_ecriture" {
  description = "Point de terminaison d'écriture de DocumentDB."
  value       = module.documentdb.point_ecriture
}

output "msk_serveurs_amorce_iam" {
  description = "Adresses Kafka (SASL/IAM)."
  value       = module.msk.serveurs_amorce_iam
}

output "redshift_workgroup" {
  description = "Nom du workgroup Redshift Serverless."
  value       = module.redshift.workgroup_nom
}

output "lac_bucket" {
  description = "Bucket du lac de données."
  value       = module.lac.lac_id
}

output "audit_bucket" {
  description = "Bucket de l'archive d'audit verrouillée."
  value       = module.lac.audit_id
}

output "roles" {
  description = "ARN des rôles IAM par fonction."
  value = {
    etl         = module.iam.role_etl_arn
    cdc         = module.iam.role_cdc_arn
    redshift_s3 = module.iam.role_redshift_s3_arn
    application = module.iam.role_application_arn
    score       = module.iam.role_score_arn
    analyste    = module.iam.role_analyste_arn
  }
}

output "secrets" {
  description = "ARN des secrets applicatifs."
  value = {
    sel_pseudonymisation = module.secrets.arn_sel_pseudonymisation
    cdc                  = module.secrets.arn_cdc
  }
}

output "calcul" {
  description = "Cluster ECS, service de score et définition de tâche de l'ETL."
  value = {
    cluster        = module.calcul.cluster_nom
    service_score  = module.calcul.service_score_nom
    definition_etl = module.calcul.definition_etl_arn
  }
}

output "supervision" {
  description = "Rubrique d'alerte et alarmes."
  value = {
    rubrique = module.supervision.rubrique_alertes_arn
    alarmes  = module.supervision.alarmes
  }
}
