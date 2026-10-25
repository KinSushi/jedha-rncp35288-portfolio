# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

output "arns" {
  description = "ARN de chaque clé, par domaine."
  value       = { for domaine, cle in aws_kms_key.cle : domaine => cle.arn }
}

output "ids" {
  description = "Identifiant de chaque clé, par domaine."
  value       = { for domaine, cle in aws_kms_key.cle : domaine => cle.key_id }
}

output "alias" {
  description = "Alias de chaque clé, par domaine."
  value       = { for domaine, alias in aws_kms_alias.cle : domaine => alias.name }
}
