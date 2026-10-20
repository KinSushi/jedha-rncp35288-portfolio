# Copyright © 2026 · voir le fichier LICENSE du dépôt

variable "prefixe" {
  description = "Préfixe des noms de rôles (projet et environnement)."
  type        = string
}

variable "region" {
  description = "Région AWS (ARN des ressources et restriction kms:ViaService)."
  type        = string
}

variable "lac_arn" {
  description = "ARN du bucket du lac de données."
  type        = string
}

variable "cle_lac_arn" {
  description = "ARN de la clé KMS du lac."
  type        = string
}

variable "cle_secrets_arn" {
  description = "ARN de la clé KMS des secrets."
  type        = string
}

variable "secrets_traitement_arns" {
  description = "ARN des secrets que le rôle etl peut lire (sel de pseudonymisation, par exemple)."
  type        = list(string)
}

variable "secret_cdc_arn" {
  description = "ARN du secret du compte de réplication Debezium."
  type        = string
}

variable "msk_cluster_arn" {
  description = "ARN du cluster MSK Serverless."
  type        = string
}

variable "aurora_cluster_resource_id" {
  description = "Identifiant de ressource du cluster Aurora (ARN rds-db:connect)."
  type        = string
}

variable "redshift_workgroup_arn" {
  description = "ARN du workgroup Redshift Serverless."
  type        = string
}

variable "principaux_analystes" {
  description = "ARN des identités (rôles fédérés, utilisateurs) autorisées à endosser le rôle analyste, avec MFA. Liste vide : rôle non créé."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Étiquettes supplémentaires."
  type        = map(string)
  default     = {}
}
