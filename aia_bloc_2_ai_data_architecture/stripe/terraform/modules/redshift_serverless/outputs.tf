# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

output "workgroup_arn" {
  description = "ARN du workgroup (sert aux politiques IAM redshift-serverless:*)."
  value       = aws_redshiftserverless_workgroup.this.arn
}

output "workgroup_nom" {
  description = "Nom du workgroup."
  value       = aws_redshiftserverless_workgroup.this.workgroup_name
}

output "namespace_arn" {
  description = "ARN du namespace."
  value       = aws_redshiftserverless_namespace.this.arn
}

output "point_de_connexion" {
  description = "Adresse et port du workgroup (liste d'objets endpoint)."
  value       = aws_redshiftserverless_workgroup.this.endpoint
}
