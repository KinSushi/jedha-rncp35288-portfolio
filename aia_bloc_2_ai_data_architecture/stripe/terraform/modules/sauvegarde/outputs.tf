# Copyright © 2026 · voir le fichier LICENSE du dépôt

output "coffre_arn" {
  description = "ARN du coffre de sauvegarde."
  value       = aws_backup_vault.this.arn
}

output "plan_id" {
  description = "Identifiant du plan de sauvegarde."
  value       = aws_backup_plan.this.id
}
