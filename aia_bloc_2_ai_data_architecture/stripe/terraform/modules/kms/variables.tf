# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

variable "prefixe" {
  description = "Préfixe des noms de ressources (projet et environnement), par exemple « stripe-dev »."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.prefixe))
    error_message = "Le préfixe doit commencer par une lettre minuscule et ne contenir que des minuscules, chiffres et tirets (3 à 31 caractères)."
  }
}

variable "region" {
  description = "Région AWS (nécessaire à la politique des clés destinées aux journaux CloudWatch)."
  type        = string
}

variable "cles" {
  description = "Clés à créer, par domaine : description, indicateur d'usage par CloudWatch Logs et indicateur de rubrique d'alerte (CloudWatch et événements RDS y publient)."
  type = map(object({
    description         = string
    journaux_cloudwatch = optional(bool, false)
    alertes_sns         = optional(bool, false)
  }))
}

variable "fenetre_suppression_jours" {
  description = "Délai (7 à 30 jours) entre la demande de suppression d'une clé et sa suppression effective : filet de sécurité contre l'erreur humaine."
  type        = number
  default     = 30

  validation {
    condition     = var.fenetre_suppression_jours >= 7 && var.fenetre_suppression_jours <= 30
    error_message = "AWS impose un délai compris entre 7 et 30 jours."
  }
}

variable "tags" {
  description = "Étiquettes supplémentaires appliquées aux clés."
  type        = map(string)
  default     = {}
}
