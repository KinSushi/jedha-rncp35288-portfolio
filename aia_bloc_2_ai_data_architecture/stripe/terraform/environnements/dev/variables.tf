# Copyright © 2026 · voir le fichier LICENSE du dépôt

variable "projet" {
  description = "Nom du projet, préfixe de toutes les ressources."
  type        = string
  default     = "stripe"
}

variable "region" {
  description = "Région AWS. HYPOTHÈSE : eu-west-1 (Irlande), en Union européenne, pour la souveraineté des données ; à confirmer (disponibilité de MSK Serverless et de Redshift Serverless dans la région choisie)."
  type        = string
  default     = "eu-west-1"
}

variable "zones" {
  description = "Zones de disponibilité (trois au minimum). Valeurs par défaut cohérentes avec la région par défaut ; à adapter si la région change."
  type        = list(string)
  default     = ["eu-west-1a", "eu-west-1b", "eu-west-1c"]
}

variable "cidr_vpc" {
  description = "Plage d'adresses du VPC. À coordonner avec le plan d'adressage de l'entreprise."
  type        = string
  default     = "10.40.0.0/16"
}

variable "fonction_rotation_arn" {
  description = "ARN de la fonction Lambda de rotation du secret Debezium. null en développement : pas de rotation."
  type        = string
  default     = null
}

variable "principaux_analystes" {
  description = "ARN des identités autorisées à endosser le rôle analyste (avec MFA). Vide : rôle non créé."
  type        = list(string)
  default     = []
}

variable "depots_ecr_arns" {
  description = "ARN des dépôts ECR qui contiennent les images du score et de l'ETL (construites par l'intégration continue, hors de ce dépôt)."
  type        = list(string)
}

variable "image_score" {
  description = "Image du service de score, désignée par son empreinte (<dépôt>@sha256:<empreinte>)."
  type        = string
}

variable "image_etl" {
  description = "Image des tâches d'ETL et de réentraînement, désignée par son empreinte."
  type        = string
}

variable "adresses_astreinte" {
  description = "Adresses électroniques prévenues par les alarmes (abonnement à confirmer par chaque destinataire). Vide : aucun abonné."
  type        = list(string)
  default     = []
}
