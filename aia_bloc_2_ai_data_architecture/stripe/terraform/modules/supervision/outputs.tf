# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

output "rubrique_alertes_arn" {
  description = "ARN de la rubrique SNS des alertes (d'autres alarmes peuvent s'y adresser)."
  value       = aws_sns_topic.alertes.arn
}

output "alarmes" {
  description = "Noms des alarmes CloudWatch créées, par risque."
  value = {
    retard_emplacement_replication = aws_cloudwatch_metric_alarm.retard_emplacement_replication.alarm_name
    consommation_entrepot          = aws_cloudwatch_metric_alarm.consommation_entrepot.alarm_name
    score_sans_tache               = aws_cloudwatch_metric_alarm.score_sans_tache.alarm_name
  }
}
