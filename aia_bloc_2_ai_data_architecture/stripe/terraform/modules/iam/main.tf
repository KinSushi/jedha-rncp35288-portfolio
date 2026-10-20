# Copyright © 2026 · voir le fichier LICENSE du dépôt
#
# Module « iam » : des rôles distincts, chacun limité à ce que sa fonction exige.
#
# Principes appliqués à chaque politique ci-dessous :
# - aucune action « * » ni « service:* » ; aucune ressource « * » sauf quand l'action AWS n'admet
#   pas de ressource précise (signalé au cas par cas) ;
# - chaque rôle ne peut être endossé que par le service ou les identités prévus, depuis ce compte ;
# - l'usage d'une clé KMS est restreint au service qui la sollicite (kms:ViaService) ;
# - les humains passent par un rôle endossé avec authentification multifacteur, jamais par des
#   clés d'accès permanentes.
#
# Rôles :
# - `etl`           : tâches d'extraction et de chargement (lac S3, entrepôt, secrets, lecture Kafka) ;
# - `cdc`           : connecteur Debezium (écriture des sujets stripe.* sur MSK, secret de réplication) ;
# - `redshift_s3`   : rôle endossé par Redshift pour lire le lac (COPY) ;
# - `application`   : service de paiement (connexion IAM à Aurora avec le seul compte applicatif) ;
# - `score`         : service de score de fraude (lecture des sujets stripe.* sur MSK, dans ses
#                     propres groupes de consommateurs score-*) ;
# - `analyste`      : lecture de l'entrepôt par des personnes (MFA obligatoire) ; absent si aucune
#                     identité n'est déclarée.

data "aws_caller_identity" "courant" {}

locals {
  compte = data.aws_caller_identity.courant.account_id

  # ARN des sujets et des groupes Kafka : ils se déduisent de l'ARN du cluster
  # (arn:aws:kafka:<région>:<compte>:cluster/<nom>/<uuid>).
  arn_sujets  = replace(var.msk_cluster_arn, ":cluster/", ":topic/")
  arn_groupes = replace(var.msk_cluster_arn, ":cluster/", ":group/")
}

# ------------------------------------------------------------------ confiance (qui peut endosser)

data "aws_iam_policy_document" "confiance_taches" {
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

data "aws_iam_policy_document" "confiance_connecteur" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["kafkaconnect.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.compte]
    }
  }
}

data "aws_iam_policy_document" "confiance_redshift" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["redshift.amazonaws.com", "redshift-serverless.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.compte]
    }
  }
}

data "aws_iam_policy_document" "confiance_analystes" {
  count = length(var.principaux_analystes) > 0 ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = var.principaux_analystes
    }

    # Pas de MFA, pas de rôle.
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
  }
}

# ------------------------------------------------------------------ rôle « etl »

data "aws_iam_policy_document" "etl" {
  statement {
    sid       = "LireEtEcrireLeLac"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${var.lac_arn}/*"]
  }

  statement {
    sid       = "ListerLeLac"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.lac_arn]
  }

  statement {
    sid       = "UtiliserLaCleDuLacViaS3"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [var.cle_lac_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com"]
    }
  }

  statement {
    sid       = "LireLesSecretsDuTraitement"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = var.secrets_traitement_arns
  }

  statement {
    sid       = "DechiffrerLesSecretsViaSecretsManager"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [var.cle_secrets_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
  }

  statement {
    sid       = "SeConnecterALAuroraEnLecture"
    effect    = "Allow"
    actions   = ["rds-db:connect"]
    resources = ["arn:aws:rds-db:${var.region}:${local.compte}:dbuser:${var.aurora_cluster_resource_id}/svc_extraction"]
  }

  statement {
    sid       = "ObtenirDesIdentifiantsTemporairesRedshift"
    effect    = "Allow"
    actions   = ["redshift-serverless:GetCredentials"]
    resources = [var.redshift_workgroup_arn]
  }

  statement {
    sid       = "ConsommerLesSujetsKafka"
    effect    = "Allow"
    actions   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"]
    resources = [var.msk_cluster_arn]
  }

  statement {
    sid    = "LireLesSujetsStripe"
    effect = "Allow"
    actions = [
      "kafka-cluster:DescribeTopic",
      "kafka-cluster:ReadData",
    ]
    resources = ["${local.arn_sujets}/stripe.*"]
  }

  statement {
    sid    = "GererLesGroupesDeConsommateurs"
    effect = "Allow"
    actions = [
      "kafka-cluster:DescribeGroup",
      "kafka-cluster:AlterGroup",
    ]
    resources = ["${local.arn_groupes}/etl-*"]
  }
}

resource "aws_iam_role" "etl" {
  name               = "${var.prefixe}-etl"
  description        = "Taches d'extraction et de chargement (lac, entrepot, Kafka en lecture)"
  assume_role_policy = data.aws_iam_policy_document.confiance_taches.json

  tags = var.tags
}

resource "aws_iam_role_policy" "etl" {
  name   = "droits-etl"
  role   = aws_iam_role.etl.id
  policy = data.aws_iam_policy_document.etl.json
}

# ------------------------------------------------------------------ rôle « cdc »

data "aws_iam_policy_document" "cdc" {
  statement {
    sid       = "SeConnecterAuCluster"
    effect    = "Allow"
    actions   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"]
    resources = [var.msk_cluster_arn]
  }

  statement {
    sid    = "PublierLesSujetsStripe"
    effect = "Allow"
    actions = [
      "kafka-cluster:CreateTopic",
      "kafka-cluster:DescribeTopic",
      "kafka-cluster:WriteData",
      "kafka-cluster:WriteDataIdempotently",
    ]
    resources = ["${local.arn_sujets}/stripe.*"]
  }

  # Kafka Connect conserve sa configuration, ses offsets et son état dans des sujets internes.
  statement {
    sid    = "SujetsInternesDeKafkaConnect"
    effect = "Allow"
    actions = [
      "kafka-cluster:CreateTopic",
      "kafka-cluster:DescribeTopic",
      "kafka-cluster:WriteData",
      "kafka-cluster:ReadData",
    ]
    resources = ["${local.arn_sujets}/__amazon_msk_connect_*", "${local.arn_sujets}/connect-*"]
  }

  statement {
    sid       = "GroupesDuConnecteur"
    effect    = "Allow"
    actions   = ["kafka-cluster:DescribeGroup", "kafka-cluster:AlterGroup"]
    resources = ["${local.arn_groupes}/__amazon_msk_connect_*", "${local.arn_groupes}/connect-*"]
  }

  statement {
    sid       = "LireLeSecretDeReplication"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.secret_cdc_arn]
  }

  statement {
    sid       = "DechiffrerLeSecretViaSecretsManager"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [var.cle_secrets_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cdc" {
  name               = "${var.prefixe}-cdc"
  description        = "Connecteur Debezium sur MSK Connect"
  assume_role_policy = data.aws_iam_policy_document.confiance_connecteur.json

  tags = var.tags
}

resource "aws_iam_role_policy" "cdc" {
  name   = "droits-cdc"
  role   = aws_iam_role.cdc.id
  policy = data.aws_iam_policy_document.cdc.json
}

# ------------------------------------------------------------------ rôle « redshift_s3 »

data "aws_iam_policy_document" "redshift_s3" {
  statement {
    sid       = "LireLeLac"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.lac_arn}/*"]
  }

  statement {
    sid       = "ListerLeLac"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [var.lac_arn]
  }

  statement {
    sid       = "DechiffrerLeLacViaS3"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [var.cle_lac_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "redshift_s3" {
  name               = "${var.prefixe}-redshift-s3"
  description        = "Role endosse par Redshift pour charger les fichiers du lac (COPY)"
  assume_role_policy = data.aws_iam_policy_document.confiance_redshift.json

  tags = var.tags
}

resource "aws_iam_role_policy" "redshift_s3" {
  name   = "lecture-du-lac"
  role   = aws_iam_role.redshift_s3.id
  policy = data.aws_iam_policy_document.redshift_s3.json
}

# ------------------------------------------------------------------ rôle « application »

data "aws_iam_policy_document" "application" {
  statement {
    sid       = "SeConnecterAAuroraAvecLeCompteApplicatif"
    effect    = "Allow"
    actions   = ["rds-db:connect"]
    resources = ["arn:aws:rds-db:${var.region}:${local.compte}:dbuser:${var.aurora_cluster_resource_id}/svc_application"]
  }
}

resource "aws_iam_role" "application" {
  name               = "${var.prefixe}-application"
  description        = "Service de paiement : connexion IAM a Aurora avec le seul compte applicatif"
  assume_role_policy = data.aws_iam_policy_document.confiance_taches.json

  tags = var.tags
}

resource "aws_iam_role_policy" "application" {
  name   = "acces-aurora"
  role   = aws_iam_role.application.id
  policy = data.aws_iam_policy_document.application.json
}

# ------------------------------------------------------------------ rôle « score »

# Le service de score lit les paiements publiés sur le bus et ne fait rien d'autre côté AWS.
# Son accès à la base de documents passe par un compte de base dédié (DocumentDB n'utilise pas
# ces politiques IAM) : il n'apparaît donc pas ici.
data "aws_iam_policy_document" "score" {
  statement {
    sid       = "SeConnecterAuCluster"
    effect    = "Allow"
    actions   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"]
    resources = [var.msk_cluster_arn]
  }

  statement {
    sid    = "LireLesSujetsStripe"
    effect = "Allow"
    actions = [
      "kafka-cluster:DescribeTopic",
      "kafka-cluster:ReadData",
    ]
    resources = ["${local.arn_sujets}/stripe.*"]
  }

  statement {
    sid    = "GererSesGroupesDeConsommateurs"
    effect = "Allow"
    actions = [
      "kafka-cluster:DescribeGroup",
      "kafka-cluster:AlterGroup",
    ]
    resources = ["${local.arn_groupes}/score-*"]
  }
}

resource "aws_iam_role" "score" {
  name               = "${var.prefixe}-score"
  description        = "Service de score de fraude : lecture des sujets stripe.* sur MSK"
  assume_role_policy = data.aws_iam_policy_document.confiance_taches.json

  tags = var.tags
}

resource "aws_iam_role_policy" "score" {
  name   = "lecture-des-paiements"
  role   = aws_iam_role.score.id
  policy = data.aws_iam_policy_document.score.json
}

# ------------------------------------------------------------------ rôle « analyste »

data "aws_iam_policy_document" "analyste" {
  count = length(var.principaux_analystes) > 0 ? 1 : 0

  statement {
    # Droit de demander des identifiants temporaires au workgroup ; les droits dans la base
    # (lecture seule sur l'étoile) sont portés par un rôle Redshift (role_analyste, voir
    # sql/olap_schema.sql), jamais par le compte administrateur.
    sid       = "InterrogerLEntrepot"
    effect    = "Allow"
    actions   = ["redshift-serverless:GetCredentials"]
    resources = [var.redshift_workgroup_arn]
  }
}

resource "aws_iam_role" "analyste" {
  count = length(var.principaux_analystes) > 0 ? 1 : 0

  name                 = "${var.prefixe}-analyste"
  description          = "Lecture de l'entrepot par des personnes, avec MFA"
  assume_role_policy   = data.aws_iam_policy_document.confiance_analystes[0].json
  max_session_duration = 3600

  tags = var.tags
}

resource "aws_iam_role_policy" "analyste" {
  count = length(var.principaux_analystes) > 0 ? 1 : 0

  name   = "lecture-entrepot"
  role   = aws_iam_role.analyste[0].id
  policy = data.aws_iam_policy_document.analyste[0].json
}
