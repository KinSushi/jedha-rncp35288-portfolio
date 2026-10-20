# Copyright © 2026 · voir le fichier LICENSE du dépôt

variable "prefixe" {
  description = "Préfixe des noms de ressources (projet et environnement)."
  type        = string
}

variable "region" {
  description = "Région AWS (sert à nommer les points de terminaison de services)."
  type        = string
}

variable "cidr" {
  description = "Plage d'adresses du VPC. Valeur par défaut : HYPOTHÈSE, à coordonner avec le plan d'adressage de l'entreprise pour éviter tout recouvrement."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrhost(var.cidr, 0))
    error_message = "Le CIDR du VPC n'est pas valide."
  }
}

variable "zones" {
  description = "Noms des zones de disponibilité à utiliser (un sous-réseau privé par zone). Trois au minimum : Redshift Serverless exige des sous-réseaux dans trois zones. Les noms dépendent de la région."
  type        = list(string)

  validation {
    condition     = length(var.zones) >= 3 && length(var.zones) <= 6 && length(distinct(var.zones)) == length(var.zones)
    error_message = "Indiquer entre 3 et 6 zones distinctes."
  }
}

variable "cle_journaux_arn" {
  description = "ARN de la clé KMS qui chiffre les journaux de flux (clé du domaine « journaux »)."
  type        = string
}

variable "retention_journaux_jours" {
  description = "Durée de conservation des journaux de flux. 365 jours : l'exigence 10.5.1 de PCI-DSS v4.0.1 demande au moins 12 mois d'historique de journaux d'audit."
  type        = number
  default     = 365

  validation {
    condition     = var.retention_journaux_jours >= 365
    error_message = "La rétention doit être d'au moins 365 jours (12 mois)."
  }
}

variable "services_points_terminaison" {
  description = "Services AWS joints par un point de terminaison d'interface (suffixe du nom de service)."
  type        = list(string)
  # ecr.api et ecr.dkr : les tâches Fargate du module calcul tirent leurs images du registre
  # privé sans passer par Internet (les couches d'image, elles, viennent de S3 par la passerelle).
  default = ["secretsmanager", "kms", "logs", "sts", "ecr.api", "ecr.dkr"]
}

variable "tags" {
  description = "Étiquettes supplémentaires."
  type        = map(string)
  default     = {}
}
