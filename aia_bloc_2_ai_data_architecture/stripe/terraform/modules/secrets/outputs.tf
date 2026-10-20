# Copyright © 2026 · voir le fichier LICENSE du dépôt

output "arn_sel_pseudonymisation" {
  description = "ARN du secret qui contient le sel de pseudonymisation."
  value       = aws_secretsmanager_secret.sel.arn
}

output "arn_cdc" {
  description = "ARN du secret du compte de réplication Debezium."
  value       = aws_secretsmanager_secret.cdc.arn
}

output "arns" {
  description = "ARN de tous les secrets du module."
  value       = [aws_secretsmanager_secret.sel.arn, aws_secretsmanager_secret.cdc.arn]
}
