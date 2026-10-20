# Copyright © 2026 · voir le fichier LICENSE du dépôt

output "id" {
  description = "Nom (identifiant) du bucket."
  value       = aws_s3_bucket.this.id
}

output "arn" {
  description = "ARN du bucket."
  value       = aws_s3_bucket.this.arn
}
