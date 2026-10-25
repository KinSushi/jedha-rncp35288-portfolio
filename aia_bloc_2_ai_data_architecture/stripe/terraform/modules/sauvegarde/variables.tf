# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

variable "prefixe" {
  description = "Préfixe des noms de ressources (projet et environnement)."
  type        = string
}

variable "cle_kms_arn" {
  description = "ARN de la clé KMS du domaine « sauvegardes » (chiffrement du coffre)."
  type        = string
}

variable "cles_sources_arns" {
  description = "ARN des clés KMS qui chiffrent les ressources sauvegardées (Aurora, DocumentDB)."
  type        = list(string)
}

variable "ressources_arns" {
  description = "ARN des ressources à sauvegarder (clusters Aurora et DocumentDB)."
  type        = list(string)
}

variable "planification" {
  description = "Calendrier des sauvegardes (expression cron AWS, en UTC). Valeur par défaut : HYPOTHÈSE, une fois par jour à 03:00, hors du pic de paiements attendu."
  type        = string
  default     = "cron(0 3 * * ? *)"
}

variable "retention_jours" {
  description = "Durée de conservation des points de restauration. HYPOTHÈSE de départ, à fixer selon la politique de rétention et le RGPD (minimisation des durées de conservation)."
  type        = number
  default     = 35
}

variable "tags" {
  description = "Étiquettes supplémentaires."
  type        = map(string)
  default     = {}
}
