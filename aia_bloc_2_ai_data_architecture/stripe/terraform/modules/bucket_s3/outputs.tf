# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

output "id" {
  description = "Nom (identifiant) du bucket."
  value       = aws_s3_bucket.this.id
}

output "arn" {
  description = "ARN du bucket."
  value       = aws_s3_bucket.this.arn
}
