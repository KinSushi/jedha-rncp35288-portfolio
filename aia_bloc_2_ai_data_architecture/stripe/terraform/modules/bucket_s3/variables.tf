# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

variable "nom" {
  description = "Nom du bucket (unique dans tout AWS : y inclure l'identifiant du compte)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.nom))
    error_message = "Nom de bucket invalide (minuscules, chiffres, points et tirets ; 3 à 63 caractères)."
  }
}

variable "cle_kms_arn" {
  description = "ARN de la clé KMS du client qui chiffre le bucket. null : chiffrement SSE-S3 (cas du bucket de journaux d'accès, dont l'écrivain S3 ne sait pas utiliser SSE-KMS)."
  type        = string
  default     = null
}

variable "bucket_journaux_id" {
  description = "Bucket qui reçoit les journaux d'accès de celui-ci. null : pas de journalisation d'accès."
  type        = string
  default     = null
}

variable "verrouillage_jours" {
  description = "Durée de rétention du verrouillage d'objets (mode gouvernance), en jours. null : pas de verrouillage."
  type        = number
  default     = null
}

variable "transitions" {
  description = "Bascules vers des classes de stockage moins chères : liste de {jours, classe}. Valeurs par défaut : aucune."
  type = list(object({
    jours  = number
    classe = string
  }))
  default = []
}

variable "versions_anciennes_jours" {
  description = "Délai avant suppression des versions non courantes d'un objet. Valeur par défaut : HYPOTHÈSE à recaler sur la politique de rétention de l'entreprise."
  type        = number
  default     = 90
}

variable "autoriser_destruction" {
  description = "Autorise `terraform destroy` à vider puis supprimer le bucket. À réserver à l'environnement de développement."
  type        = bool
  default     = false
}

variable "politique_additionnelle_json" {
  description = "Politique de bucket JSON fusionnée à la politique de base (par exemple l'autorisation du service de journalisation)."
  type        = string
  default     = null
}

variable "tags" {
  description = "Étiquettes supplémentaires."
  type        = map(string)
  default     = {}
}
