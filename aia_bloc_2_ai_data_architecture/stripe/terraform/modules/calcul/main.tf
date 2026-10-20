# Copyright © 2026 · voir le fichier LICENSE du dépôt
#
# Module « calcul » : les tâches de calcul, en conteneurs sans serveur (ECS sur Fargate).
#
# Deux charges, deux formes :
# - le service de score (consommateur Kafka qui note chaque paiement) tourne EN CONTINU : c'est
#   un service ECS, réparti sur les sous-réseaux privés de plusieurs zones, dont le nombre de
#   tâches suit la charge (autoscaling par suivi de cible sur le CPU) entre un minimum et un
#   maximum ;
# - l'ETL et le réentraînement tournent À LA DEMANDE : une simple définition de tâche, lancée
#   chaque jour par l'orchestrateur (DAG stripe/dags/stripe_quotidien.py), sans service permanent.
#
# Pourquoi Fargate (note d'arbitrage, document 01 § 7.1) : pas de serveurs à patcher ni à
# dimensionner, mémoire et CPU déclarés par tâche ; écartés : EKS (un cluster Kubernetes à
# exploiter pour deux charges) et Lambda (durée bornée à 15 minutes, trop courte pour
# l'entraînement en S3).
#
# Sécurité, identique aux autres modules : aucune adresse publique, groupe de sécurité
# « application » du module reseau, système de fichiers en lecture seule, utilisateur sans
# privilège, journaux chiffrés et conservés 12 mois, images désignées par leur empreinte
# (sha256) et non par une étiquette modifiable.
#
# Toutes les tailles (CPU, mémoire, bornes de l'autoscaling, cible de CPU) sont des HYPOTHÈSES
# (H16, document 09 § 2 et § 4.5) : les valeurs viennent des environnements.

data "aws_caller_identity" "courant" {}

locals {
  compte = data.aws_caller_identity.courant.account_id

  # Réglages communs aux conteneurs : rien n'écrit dans l'image, aucun privilège. Architecture
  # x86_64 (celle des images construites par le Dockerfile du dépôt) ; ARM64 (Graviton, moins
  # cher et plus sobre) exigerait une image multi-architecture : piste GreenOps, document 09 § 6.
  conteneur_durci = {
    essential              = true
    readonlyRootFilesystem = true
    privileged             = false
    user                   = "10001:10001" # l'utilisateur « appli » du Dockerfile
    # Seul endroit inscriptible : un volume éphémère de la tâche monté sur /tmp (l'équivalent
    # du tmpfs du docker-compose).
    mountPoints = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
    linuxParameters = {
      initProcessEnabled = true
      capabilities       = { drop = ["ALL"] }
    }
  }
}

# ------------------------------------------------------------------ cluster

resource "aws_ecs_cluster" "this" {
  name = "${var.prefixe}-calcul"

  # Container Insights : métriques CPU, mémoire et réseau par tâche, lues par l'autoscaling et
  # par les tableaux de bord (document 10 § 4).
  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  # Session « ECS Exec » (diagnostic) : journalisée et chiffrée par la clé des journaux.
  configuration {
    execute_command_configuration {
      kms_key_id = var.cle_journaux_arn
      logging    = "OVERRIDE"

      log_configuration {
        cloud_watch_encryption_enabled = true
        cloud_watch_log_group_name     = aws_cloudwatch_log_group.exec.name
      }
    }
  }

  tags = var.tags
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

# ------------------------------------------------------------------ journaux

resource "aws_cloudwatch_log_group" "taches" {
  for_each = toset(["score", "etl"])

  name              = "/ecs/${var.prefixe}/${each.key}"
  retention_in_days = var.retention_journaux_jours
  kms_key_id        = var.cle_journaux_arn

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "exec" {
  name              = "/ecs/${var.prefixe}/exec"
  retention_in_days = var.retention_journaux_jours
  kms_key_id        = var.cle_journaux_arn

  tags = var.tags
}

# ------------------------------------------------------------------ rôle d'exécution

# Le rôle d'EXÉCUTION sert à ECS lui-même (tirer l'image, écrire les journaux). Il est distinct
# des rôles de TÂCHE (etl, score), qui portent les droits du code applicatif (module iam).
data "aws_iam_policy_document" "confiance_execution" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.compte]
    }
  }
}

data "aws_iam_policy_document" "execution" {
  # Seule exception du module : ecr:GetAuthorizationToken n'admet aucune ressource précise
  # (le jeton vaut pour le registre du compte entier), la ressource est donc « * ».
  statement {
    sid       = "JetonDuRegistre"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid       = "TirerLesImagesDesDepotsDuProjet"
    effect    = "Allow"
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = var.depots_ecr_arns
  }

  statement {
    sid       = "EcrireLesJournauxDesTaches"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [for groupe in aws_cloudwatch_log_group.taches : "${groupe.arn}:*"]
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.prefixe}-ecs-execution"
  description        = "Role d'execution ECS : tirer les images du projet et ecrire les journaux"
  assume_role_policy = data.aws_iam_policy_document.confiance_execution.json

  tags = var.tags
}

resource "aws_iam_role_policy" "execution" {
  name   = "execution-taches"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ------------------------------------------------------------------ définitions de tâches

resource "aws_ecs_task_definition" "score" {
  family                   = "${var.prefixe}-score"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.taches["score"].cpu
  memory                   = var.taches["score"].memoire
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = var.role_score_arn

  volume {
    name = "tmp"
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([merge(local.conteneur_durci, {
    name        = "score"
    image       = var.image_score
    environment = [for nom, valeur in var.environnement_score : { name = nom, value = valeur }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.taches["score"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "score"
      }
    }
  })])

  tags = var.tags
}

resource "aws_ecs_task_definition" "etl" {
  family                   = "${var.prefixe}-etl"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.taches["etl"].cpu
  memory                   = var.taches["etl"].memoire
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = var.role_etl_arn

  volume {
    name = "tmp"
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([merge(local.conteneur_durci, {
    name        = "etl"
    image       = var.image_etl
    environment = [for nom, valeur in var.environnement_etl : { name = nom, value = valeur }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.taches["etl"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "etl"
      }
    }
  })])

  tags = var.tags
}

# ------------------------------------------------------------------ service de score

resource "aws_ecs_service" "score" {
  name             = "${var.prefixe}-score"
  cluster          = aws_ecs_cluster.this.id
  task_definition  = aws_ecs_task_definition.score.arn
  launch_type      = "FARGATE"
  platform_version = "LATEST"
  desired_count    = var.score_taches_min

  # Déploiement progressif : les nouvelles tâches démarrent avant l'arrêt des anciennes, jamais
  # moins que la capacité nominale ; un déploiement qui échoue revient seul à la révision
  # précédente (document 10 § 6.1).
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.sous_reseaux_ids
    security_groups  = [var.groupe_securite_id]
    assign_public_ip = false
  }

  propagate_tags = "SERVICE"

  # Le nombre de tâches appartient ensuite à l'autoscaling : Terraform ne doit pas le remettre
  # au minimum à chaque application.
  lifecycle {
    ignore_changes = [desired_count]
  }

  tags = var.tags
}

# ------------------------------------------------------------------ autoscaling du score

resource "aws_appautoscaling_target" "score" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.score.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.score_taches_min
  max_capacity       = var.score_taches_max

  lifecycle {
    precondition {
      condition     = var.score_taches_max >= var.score_taches_min
      error_message = "score_taches_max doit être supérieur ou égal à score_taches_min."
    }
  }
}

resource "aws_appautoscaling_policy" "score_cpu" {
  name               = "${var.prefixe}-score-cpu"
  service_namespace  = aws_appautoscaling_target.score.service_namespace
  resource_id        = aws_appautoscaling_target.score.resource_id
  scalable_dimension = aws_appautoscaling_target.score.scalable_dimension
  policy_type        = "TargetTrackingScaling"

  target_tracking_scaling_policy_configuration {
    target_value       = var.score_cible_cpu_pourcent
    scale_out_cooldown = var.delai_montee_s
    scale_in_cooldown  = var.delai_descente_s

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}
