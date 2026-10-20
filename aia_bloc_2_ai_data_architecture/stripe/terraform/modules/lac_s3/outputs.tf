# Copyright © 2026 · voir le fichier LICENSE du dépôt

output "lac_id" {
  description = "Nom du bucket du lac de données."
  value       = module.lac.id
}

output "lac_arn" {
  description = "ARN du bucket du lac de données."
  value       = module.lac.arn
}

output "journaux_id" {
  description = "Nom du bucket des journaux d'accès."
  value       = module.journaux.id
}

output "audit_id" {
  description = "Nom du bucket de l'archive d'audit verrouillée."
  value       = module.audit.id
}

output "audit_arn" {
  description = "ARN du bucket de l'archive d'audit verrouillée."
  value       = module.audit.arn
}
