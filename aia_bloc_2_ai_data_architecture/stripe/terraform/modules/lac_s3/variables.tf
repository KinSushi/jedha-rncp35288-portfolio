# Copyright © 2026 · voir le fichier LICENSE du dépôt

variable "prefixe" {
  description = "Préfixe des noms de buckets (projet et environnement)."
  type        = string
}

variable "cle_lac_arn" {
  description = "ARN de la clé KMS du domaine « lac »."
  type        = string
}

variable "cle_audit_arn" {
  description = "ARN de la clé KMS du domaine « journaux » (archive d'audit)."
  type        = string
}

variable "transition_peu_frequent_jours" {
  description = "Âge (jours) à partir duquel un objet du lac passe en classe « accès peu fréquent ». HYPOTHÈSE de départ ; S3 impose au moins 30 jours."
  type        = number
  default     = 30

  validation {
    condition     = var.transition_peu_frequent_jours >= 30
    error_message = "La bascule vers STANDARD_IA exige au moins 30 jours."
  }
}

variable "transition_froid_jours" {
  description = "Âge (jours) à partir duquel un objet passe en archivage (GLACIER). HYPOTHÈSE de départ."
  type        = number
  default     = 90
}

variable "versions_anciennes_jours" {
  description = "Délai avant suppression des versions non courantes dans le lac. HYPOTHÈSE de départ."
  type        = number
  default     = 90
}

variable "retention_audit_jours" {
  description = "Rétention verrouillée de l'archive d'audit. 365 jours : PCI-DSS v4.0.1, exigence 10.5.1 (au moins 12 mois d'historique)."
  type        = number
  default     = 365

  validation {
    condition     = var.retention_audit_jours >= 365
    error_message = "La rétention de l'archive d'audit doit être d'au moins 365 jours."
  }
}

variable "autoriser_destruction" {
  description = "Autorise la destruction du lac et des journaux avec leur contenu (développement uniquement). L'archive d'audit reste toujours protégée."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Étiquettes supplémentaires."
  type        = map(string)
  default     = {}
}
