# Copyright © 2026 · voir le fichier LICENSE du dépôt

variable "prefixe" {
  description = "Préfixe des noms de ressources (projet et environnement)."
  type        = string
}

variable "sous_reseaux_ids" {
  description = "Sous-réseaux privés du workgroup. Redshift Serverless exige des sous-réseaux dans trois zones de disponibilité."
  type        = list(string)

  validation {
    condition     = length(var.sous_reseaux_ids) >= 3
    error_message = "Redshift Serverless exige au moins trois sous-réseaux, dans trois zones."
  }
}

variable "groupe_securite_id" {
  description = "Groupe de sécurité de l'entrepôt (module reseau)."
  type        = string
}

variable "cle_kms_arn" {
  description = "ARN de la clé KMS du domaine « analytique » (chiffrement des données)."
  type        = string
}

variable "cle_secrets_arn" {
  description = "ARN de la clé KMS du domaine « secrets » (mot de passe administrateur géré par Secrets Manager)."
  type        = string
}

variable "role_s3_arn" {
  description = "ARN du rôle IAM que Redshift endosse pour lire le lac (COPY) : module iam."
  type        = string
}

variable "nom_base" {
  description = "Nom de la base créée dans le namespace."
  type        = string
  default     = "stripe_dwh"
}

variable "administrateur" {
  description = "Nom du compte administrateur (son mot de passe est géré par Secrets Manager)."
  type        = string
  default     = "dwh_admin"
}

variable "capacite_base_rpu" {
  description = "Capacité de base du workgroup en RPU (unités de calcul Redshift). HYPOTHÈSE de dimensionnement : voir livrables/aia2/09_dimensionnement_finops_greenops.md."
  type        = number
}

variable "capacite_max_rpu" {
  description = "Capacité maximale en RPU (plafond de la montée en charge automatique). HYPOTHÈSE de dimensionnement."
  type        = number
}

variable "duree_max_requete_s" {
  description = "Durée maximale d'une requête, en secondes. HYPOTHÈSE de départ."
  type        = number
  default     = 3600
}

variable "plafond_rpu_heures_mensuel" {
  description = "Plafond mensuel de consommation de calcul, en RPU-heures. HYPOTHÈSE de départ, à fixer avec la direction financière (voir la note FinOps)."
  type        = number
}

variable "action_depassement" {
  description = "Action au dépassement du plafond : log, emit-metric ou deactivate."
  type        = string
  default     = "emit-metric"

  validation {
    condition     = contains(["log", "emit-metric", "deactivate"], var.action_depassement)
    error_message = "Valeurs admises : log, emit-metric, deactivate."
  }
}

variable "tags" {
  description = "Étiquettes supplémentaires."
  type        = map(string)
  default     = {}
}
