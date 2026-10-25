# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

variable "prefixe" {
  description = "Préfixe des noms de ressources (projet et environnement)."
  type        = string
}

variable "sous_reseaux_ids" {
  description = "Sous-réseaux privés du groupe de sous-réseaux de la base (au moins deux zones)."
  type        = list(string)

  validation {
    condition     = length(var.sous_reseaux_ids) >= 2
    error_message = "Aurora exige des sous-réseaux dans au moins deux zones de disponibilité."
  }
}

variable "zones" {
  description = "Zones de disponibilité où placer les instances, dans l'ordre de préférence."
  type        = list(string)
}

variable "groupe_securite_id" {
  description = "Groupe de sécurité de la base (module reseau)."
  type        = string
}

variable "cle_kms_arn" {
  description = "ARN de la clé KMS du domaine « oltp » (stockage, Performance Insights)."
  type        = string
}

variable "cle_secrets_arn" {
  description = "ARN de la clé KMS du domaine « secrets » (mot de passe administrateur géré par Secrets Manager)."
  type        = string
}

variable "cle_journaux_arn" {
  description = "ARN de la clé KMS du domaine « journaux » (groupe de journaux CloudWatch)."
  type        = string
}

variable "retention_journaux_jours" {
  description = "Rétention des journaux PostgreSQL dans CloudWatch. 365 jours : PCI-DSS v4.0.1, exigence 10.5.1."
  type        = number
  default     = 365
}

variable "version_moteur" {
  description = "Version d'Aurora PostgreSQL (famille 16). HYPOTHÈSE : à remplacer par une version proposée par la région au moment du déploiement (`aws rds describe-db-engine-versions`)."
  type        = string
  default     = "16.4"
}

variable "classe_instance" {
  description = "Classe des instances. HYPOTHÈSE de dimensionnement : voir livrables/aia2/09_dimensionnement_finops_greenops.md."
  type        = string
}

variable "nb_instances" {
  description = "Nombre d'instances : 1 en écriture + (n-1) lecteurs. Deux au minimum en production pour disposer d'un basculement."
  type        = number
  default     = 2

  validation {
    condition     = var.nb_instances >= 1 && var.nb_instances <= 15
    error_message = "Aurora accepte de 1 à 15 instances par cluster."
  }
}

variable "nom_base" {
  description = "Nom de la base créée dans le cluster."
  type        = string
  default     = "stripe"
}

variable "administrateur" {
  description = "Nom du compte administrateur (son mot de passe est géré par Secrets Manager)."
  type        = string
  default     = "stripe_admin"
}

variable "retention_sauvegardes_jours" {
  description = "Durée de conservation des sauvegardes automatiques (1 à 35 jours)."
  type        = number
  default     = 35

  validation {
    condition     = var.retention_sauvegardes_jours >= 1 && var.retention_sauvegardes_jours <= 35
    error_message = "Aurora conserve les sauvegardes de 1 à 35 jours."
  }
}

variable "fenetre_sauvegarde" {
  description = "Fenêtre quotidienne de sauvegarde (UTC)."
  type        = string
  default     = "01:00-02:00"
}

variable "fenetre_maintenance" {
  description = "Fenêtre hebdomadaire de maintenance (UTC), hors des pics de paiement attendus."
  type        = string
  default     = "sun:03:00-sun:04:00"
}

variable "protection_suppression" {
  description = "Active la protection contre la suppression et exige un instantané final. À désactiver uniquement en développement."
  type        = bool
  default     = true
}

variable "seuil_requete_lente_ms" {
  description = "Durée (ms) au-delà de laquelle une requête est journalisée. HYPOTHÈSE de départ, à régler selon l'objectif de latence."
  type        = number
  default     = 1000
}

variable "tags" {
  description = "Étiquettes supplémentaires."
  type        = map(string)
  default     = {}
}
