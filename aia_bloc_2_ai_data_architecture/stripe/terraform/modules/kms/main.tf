# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
#
# Module « kms » : une clé de chiffrement gérée par le client (CMK) PAR DOMAINE DE DONNÉES.
#
# Pourquoi une clé par domaine (base transactionnelle, lac, entrepôt, documents, secrets,
# journaux) plutôt qu'une clé unique :
# - segmentation : compromettre ou révoquer l'accès à une clé n'expose qu'un domaine ;
# - moindre privilège : chaque rôle IAM ne reçoit le droit d'utiliser que les clés de son périmètre ;
# - traçabilité : CloudTrail montre quelle clé a servi pour quel domaine.
# La rotation automatique annuelle est activée sur toutes les clés.

data "aws_caller_identity" "courant" {}

locals {
  compte = data.aws_caller_identity.courant.account_id
}

data "aws_iam_policy_document" "cle" {
  #checkov:skip=CKV_AWS_109:Politique de cle recommandee par AWS : seul le compte racine recoit kms:* pour deleguer a IAM ; dans une politique de cle, la ressource * designe la cle elle-meme.
  #checkov:skip=CKV_AWS_111:Voir CKV_AWS_109 : meme statement de delegation a IAM, sans autre principal.
  #checkov:skip=CKV_AWS_356:Voir CKV_AWS_109 : * designe uniquement la cle porteuse de la politique.
  for_each = var.cles

  # Gestion déléguée à IAM : les droits d'usage s'accordent ensuite par politiques IAM, au plus
  # juste, plutôt que dans la politique de la clé. C'est la forme de politique recommandée par AWS.
  statement {
    sid       = "DelegationAIam"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.compte}:root"]
    }
  }

  # Les groupes de journaux CloudWatch chiffrés par la clé exigent que le service Logs puisse
  # l'utiliser, limité aux groupes de ce compte et de cette région.
  dynamic "statement" {
    for_each = each.value.journaux_cloudwatch ? [1] : []

    content {
      sid    = "JournauxCloudWatch"
      effect = "Allow"
      actions = [
        "kms:Encrypt*",
        "kms:Decrypt*",
        "kms:ReEncrypt*",
        "kms:GenerateDataKey*",
        "kms:Describe*",
      ]
      resources = ["*"]

      principals {
        type        = "Service"
        identifiers = ["logs.${var.region}.amazonaws.com"]
      }

      condition {
        test     = "ArnLike"
        variable = "kms:EncryptionContext:aws:logs:arn"
        values   = ["arn:aws:logs:${var.region}:${local.compte}:*"]
      }
    }
  }

  # La rubrique SNS des alertes est chiffrée par cette clé : CloudWatch (alarmes) et les
  # événements RDS doivent pouvoir chiffrer les messages qu'ils y publient. Sans ce droit, une
  # alarme passerait à l'état ALARM sans que personne ne soit prévenu.
  dynamic "statement" {
    for_each = each.value.alertes_sns ? [1] : []

    content {
      sid       = "PublicationDesAlertes"
      effect    = "Allow"
      actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]
      resources = ["*"]

      principals {
        type        = "Service"
        identifiers = ["cloudwatch.amazonaws.com", "events.rds.amazonaws.com"]
      }
    }
  }
}

resource "aws_kms_key" "cle" {
  for_each = var.cles

  description             = "${var.prefixe} : ${each.value.description}"
  enable_key_rotation     = true
  deletion_window_in_days = var.fenetre_suppression_jours
  policy                  = data.aws_iam_policy_document.cle[each.key].json

  tags = merge(var.tags, { domaine = each.key })
}

resource "aws_kms_alias" "cle" {
  for_each = var.cles

  name          = "alias/${var.prefixe}-${each.key}"
  target_key_id = aws_kms_key.cle[each.key].key_id
}
