# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

output "cluster_nom" {
  description = "Nom du cluster ECS (dimension des métriques et cible des tâches lancées par l'orchestrateur)."
  value       = aws_ecs_cluster.this.name
}

output "cluster_arn" {
  description = "ARN du cluster ECS."
  value       = aws_ecs_cluster.this.arn
}

output "service_score_nom" {
  description = "Nom du service de score (dimension des métriques ECS)."
  value       = aws_ecs_service.score.name
}

output "definition_etl_arn" {
  description = "ARN de la définition de tâche de l'ETL, lancée chaque jour par l'orchestrateur."
  value       = aws_ecs_task_definition.etl.arn
}

output "role_execution_arn" {
  description = "ARN du rôle d'exécution ECS (tirer les images, écrire les journaux)."
  value       = aws_iam_role.execution.arn
}
