# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

output "cluster_arn" {
  description = "ARN du cluster (sert aux politiques IAM kafka-cluster:*)."
  value       = aws_msk_serverless_cluster.this.arn
}

output "cluster_uuid" {
  description = "UUID du cluster (partie de l'ARN des sujets et des groupes de consommateurs)."
  value       = aws_msk_serverless_cluster.this.cluster_uuid
}

output "serveurs_amorce_iam" {
  description = "Adresses de connexion Kafka (SASL/IAM) à fournir aux clients."
  value       = aws_msk_serverless_cluster.this.bootstrap_brokers_sasl_iam
}
