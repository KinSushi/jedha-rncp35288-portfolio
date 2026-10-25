# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

output "point_ecriture" {
  description = "Point de terminaison du cluster (écritures et lectures cohérentes)."
  value       = aws_rds_cluster.this.endpoint
}

output "point_lecture" {
  description = "Point de terminaison des lecteurs (lectures réparties)."
  value       = aws_rds_cluster.this.reader_endpoint
}

output "port" {
  description = "Port PostgreSQL."
  value       = aws_rds_cluster.this.port
}

output "cluster_arn" {
  description = "ARN du cluster."
  value       = aws_rds_cluster.this.arn
}

output "cluster_resource_id" {
  description = "Identifiant de ressource du cluster (sert aux politiques IAM d'authentification rds-db:connect)."
  value       = aws_rds_cluster.this.cluster_resource_id
}

output "secret_administrateur_arn" {
  description = "ARN du secret Secrets Manager qui contient le mot de passe administrateur."
  value       = aws_rds_cluster.this.master_user_secret[0].secret_arn
}

output "cluster_identifiant" {
  description = "Identifiant du cluster (dimension des métriques CloudWatch et source des événements RDS)."
  value       = aws_rds_cluster.this.cluster_identifier
}
