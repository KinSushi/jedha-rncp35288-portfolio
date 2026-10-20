# Copyright © 2026 · voir le fichier LICENSE du dépôt
#
# Module « supervision » : les alarmes qui comptent le plus, et la rubrique qui prévient
# l'astreinte.
#
# Chaîne d'alerte (document 10 § 4.1) : métrique CloudWatch -> alarme -> rubrique SNS chiffrée
# -> astreinte (courriel, ou tout abonné ajouté plus tard) -> procédure d'exploitation (§ 3).
# Chaque alarme prévient aussi quand la situation redevient normale (ok_actions).
#
# Alarmes retenues, une par risque majeur du document 10 § 3 :
# - retard de l'emplacement de réplication logique (panne n° 4 : le connecteur Debezium
#   s'arrête et le WAL s'accumule sur le cluster) ;
# - basculement ou défaillance du cluster Aurora (pannes n° 1 et 5), par un abonnement aux
#   événements du service plutôt que par une métrique : l'événement dit ce qui s'est passé ;
# - consommation de l'entrepôt plus rapide que le plafond mensuel ne le permet (panne n° 10) ;
# - service de score sans aucune tâche en marche (panne n° 7).
# Écartés pour l'instant : tableaux de bord, alarmes de qualité et de dérive du modèle (PSI),
# qui exigent des métriques applicatives que le code ne publie pas encore (document 13 § 5).
#
# Noms de métriques AWS (OldestReplicationSlotLag, ComputeSeconds, RunningTaskCount) : repris
# de la documentation AWS, à confirmer le jour du premier `terraform plan` (non exécuté ici).

locals {
  # Alarme de consommation journalière : un jour qui consomme plus d'un trentième du plafond
  # mensuel épuiserait le plafond avant la fin du mois. RPU-heures -> RPU-secondes.
  seuil_entrepot_rpu_secondes_par_jour = var.plafond_rpu_heures_mensuel * 3600 / 30
}

# ------------------------------------------------------------------ rubrique d'alerte

resource "aws_sns_topic" "alertes" {
  name              = "${var.prefixe}-alertes"
  kms_master_key_id = var.cle_alertes_arn

  tags = var.tags
}

resource "aws_sns_topic_subscription" "astreinte" {
  for_each = toset(var.adresses_astreinte)

  topic_arn = aws_sns_topic.alertes.arn
  protocol  = "email"
  endpoint  = each.value
}

# ------------------------------------------------------------------ capture des changements

resource "aws_cloudwatch_metric_alarm" "retard_emplacement_replication" {
  alarm_name        = "${var.prefixe}-retard-emplacement-replication"
  alarm_description = "WAL retenu par l'emplacement de replication au-dela du seuil : connecteur Debezium arrete ou lent (document 10, panne 4). Verifier avec : python -m stripe_demo.supervision"

  namespace   = "AWS/RDS"
  metric_name = "OldestReplicationSlotLag"
  dimensions = {
    DBClusterIdentifier = var.aurora_cluster_identifiant
    Role                = "WRITER"
  }
  statistic = "Maximum"

  # Deux périodes de 5 minutes : « retard croissant sur 10 minutes » (document 10 § 4.2).
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.seuil_retard_emplacement_octets
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alertes.arn]
  ok_actions    = [aws_sns_topic.alertes.arn]

  tags = var.tags
}

# ------------------------------------------------------------------ base transactionnelle

resource "aws_db_event_subscription" "aurora" {
  name        = "${var.prefixe}-aurora-evenements"
  sns_topic   = aws_sns_topic.alertes.arn
  source_type = "db-cluster"
  source_ids  = [var.aurora_cluster_identifiant]

  # Basculement (un lecteur devient l'écrivain) et défaillance du cluster.
  event_categories = ["failover", "failure"]

  tags = var.tags
}

# ------------------------------------------------------------------ entrepôt

resource "aws_cloudwatch_metric_alarm" "consommation_entrepot" {
  alarm_name        = "${var.prefixe}-consommation-entrepot"
  alarm_description = "L'entrepot a consomme en un jour plus d'un trentieme de son plafond mensuel de RPU-heures (document 10, panne 10)."

  namespace   = "AWS/Redshift-Serverless"
  metric_name = "ComputeSeconds"
  dimensions = {
    Workgroup = var.redshift_workgroup_nom
  }
  statistic = "Sum"

  period              = 86400
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = local.seuil_entrepot_rpu_secondes_par_jour
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alertes.arn]
  ok_actions    = [aws_sns_topic.alertes.arn]

  tags = var.tags
}

# ------------------------------------------------------------------ service de score

resource "aws_cloudwatch_metric_alarm" "score_sans_tache" {
  alarm_name        = "${var.prefixe}-score-sans-tache"
  alarm_description = "Aucune tache du service de score ne tourne : les paiements ne sont plus notes (document 10, panne 7)."

  namespace   = "ECS/ContainerInsights"
  metric_name = "RunningTaskCount"
  dimensions = {
    ClusterName = var.cluster_ecs_nom
    ServiceName = var.service_score_nom
  }
  statistic = "Minimum"

  period              = 60
  evaluation_periods  = 3
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  # Plus aucune donnée : le service ne publie plus rien, c'est la panne elle-même.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alertes.arn]
  ok_actions    = [aws_sns_topic.alertes.arn]

  tags = var.tags
}
