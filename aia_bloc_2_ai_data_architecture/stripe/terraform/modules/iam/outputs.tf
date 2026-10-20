# Copyright © 2026 · voir le fichier LICENSE du dépôt

output "role_etl_arn" {
  description = "ARN du rôle des tâches d'extraction et de chargement."
  value       = aws_iam_role.etl.arn
}

output "role_cdc_arn" {
  description = "ARN du rôle du connecteur Debezium."
  value       = aws_iam_role.cdc.arn
}

output "role_redshift_s3_arn" {
  description = "ARN du rôle endossé par Redshift pour lire le lac."
  value       = aws_iam_role.redshift_s3.arn
}

output "role_application_arn" {
  description = "ARN du rôle du service de paiement."
  value       = aws_iam_role.application.arn
}

output "role_score_arn" {
  description = "ARN du rôle du service de score de fraude."
  value       = aws_iam_role.score.arn
}

output "role_analyste_arn" {
  description = "ARN du rôle analyste (null si aucune identité n'est déclarée)."
  value       = length(aws_iam_role.analyste) > 0 ? aws_iam_role.analyste[0].arn : null
}
