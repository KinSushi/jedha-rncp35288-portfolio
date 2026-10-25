# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

variable "prefixe" {
  description = "Préfixe des noms de secrets (projet et environnement)."
  type        = string
}

variable "cle_kms_arn" {
  description = "ARN de la clé KMS du domaine « secrets »."
  type        = string
}

variable "fenetre_recuperation_jours" {
  description = "Délai de récupération après suppression d'un secret (7 à 30 jours, ou 0 pour supprimer immédiatement : développement uniquement)."
  type        = number
  default     = 30

  validation {
    condition     = var.fenetre_recuperation_jours == 0 || (var.fenetre_recuperation_jours >= 7 && var.fenetre_recuperation_jours <= 30)
    error_message = "Utiliser 0 (suppression immédiate) ou une valeur entre 7 et 30 jours."
  }
}

variable "fonction_rotation_arn" {
  description = "ARN de la fonction Lambda de rotation du mot de passe Debezium. null : pas de rotation (développement)."
  type        = string
  default     = null
}

variable "rotation_jours" {
  description = "Période de rotation du mot de passe, en jours. HYPOTHÈSE de départ : la fréquence est à fixer par l'analyse de risque ciblée de l'entreprise (PCI-DSS v4.0.1)."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Étiquettes supplémentaires."
  type        = map(string)
  default     = {}
}
