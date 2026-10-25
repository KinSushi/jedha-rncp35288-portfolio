# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

variable "prefixe" {
  description = "Préfixe des noms de ressources (projet et environnement)."
  type        = string
}

variable "cle_alertes_arn" {
  description = "ARN de la clé KMS de la rubrique d'alerte (module kms, domaine « alertes », ouverte à CloudWatch et aux événements RDS)."
  type        = string
}

variable "adresses_astreinte" {
  description = "Adresses électroniques abonnées à la rubrique d'alerte (chaque abonnement doit être confirmé par son destinataire). Liste vide : aucun abonné, alarmes visibles dans la console seulement."
  type        = list(string)
  default     = []
}

variable "aurora_cluster_identifiant" {
  description = "Identifiant du cluster Aurora (dimension des métriques et source des événements)."
  type        = string
}

variable "seuil_retard_emplacement_octets" {
  description = "WAL retenu par l'emplacement de réplication au-delà duquel l'astreinte est prévenue, en octets. HYPOTHÈSE H17 (document 09 § 2)."
  type        = number

  validation {
    condition     = var.seuil_retard_emplacement_octets > 0
    error_message = "Le seuil de retard doit être strictement positif."
  }
}

variable "redshift_workgroup_nom" {
  description = "Nom du workgroup Redshift Serverless (dimension de la métrique de consommation)."
  type        = string
}

variable "plafond_rpu_heures_mensuel" {
  description = "Plafond mensuel de consommation de l'entrepôt, en RPU-heures (le même que celui du module redshift_serverless, hypothèse H7)."
  type        = number

  validation {
    condition     = var.plafond_rpu_heures_mensuel > 0
    error_message = "Le plafond doit être strictement positif."
  }
}

variable "cluster_ecs_nom" {
  description = "Nom du cluster ECS du module calcul."
  type        = string
}

variable "service_score_nom" {
  description = "Nom du service de score du module calcul."
  type        = string
}

variable "tags" {
  description = "Étiquettes supplémentaires."
  type        = map(string)
  default     = {}
}
