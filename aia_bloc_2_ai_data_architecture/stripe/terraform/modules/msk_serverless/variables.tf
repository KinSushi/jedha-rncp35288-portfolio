# Copyright © 2026 · voir le fichier LICENSE du dépôt

variable "prefixe" {
  description = "Préfixe des noms de ressources (projet et environnement)."
  type        = string
}

variable "sous_reseaux_ids" {
  description = "Sous-réseaux privés du cluster (au moins deux zones de disponibilité)."
  type        = list(string)

  validation {
    condition     = length(var.sous_reseaux_ids) >= 2
    error_message = "MSK Serverless exige des sous-réseaux dans au moins deux zones de disponibilité."
  }
}

variable "groupe_securite_id" {
  description = "Groupe de sécurité du bus de messages (module reseau)."
  type        = string
}

variable "tags" {
  description = "Étiquettes supplémentaires."
  type        = map(string)
  default     = {}
}
