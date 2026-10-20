# Copyright © 2026 · voir le fichier LICENSE du dépôt

output "point_ecriture" {
  description = "Point de terminaison du cluster (écritures)."
  value       = aws_docdb_cluster.this.endpoint
}

output "point_lecture" {
  description = "Point de terminaison des réplicas (lectures réparties)."
  value       = aws_docdb_cluster.this.reader_endpoint
}

output "port" {
  description = "Port de l'API MongoDB."
  value       = aws_docdb_cluster.this.port
}

output "cluster_arn" {
  description = "ARN du cluster."
  value       = aws_docdb_cluster.this.arn
}
