# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
#
# Module « sauvegarde » : sauvegardes centralisées par AWS Backup (Aurora et DocumentDB).
#
# Pourquoi en plus des sauvegardes automatiques des bases : AWS Backup range les points de
# restauration dans un COFFRE distinct, chiffré par sa propre clé KMS (domaine « sauvegardes »),
# avec un calendrier et une rétention définis à un seul endroit. Une erreur de configuration ou une
# suppression des bases n'efface donc pas ces copies. Le coffre peut ensuite être verrouillé
# (AWS Backup Vault Lock) : option NON activée ici, car irréversible après son délai de grâce.

data "aws_iam_policy_document" "confiance" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["backup.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sauvegarde" {
  name               = "${var.prefixe}-sauvegarde"
  description        = "Role endosse par AWS Backup pour sauvegarder et restaurer les bases"
  assume_role_policy = data.aws_iam_policy_document.confiance.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "sauvegarde" {
  role       = aws_iam_role.sauvegarde.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_iam_role_policy_attachment" "restauration" {
  role       = aws_iam_role.sauvegarde.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores"
}

# Les bases sont chiffrées par leurs propres clés : le rôle de sauvegarde doit pouvoir les utiliser
# (et seulement elles) pour copier et restaurer les points de restauration.
data "aws_iam_policy_document" "cles" {
  statement {
    sid       = "UtiliserLesClesDesBases"
    effect    = "Allow"
    actions   = ["kms:DescribeKey", "kms:Decrypt", "kms:GenerateDataKey", "kms:ReEncryptFrom", "kms:ReEncryptTo"]
    resources = concat(var.cles_sources_arns, [var.cle_kms_arn])
  }

  statement {
    sid       = "AccorderDesDelegationsAuxServices"
    effect    = "Allow"
    actions   = ["kms:CreateGrant"]
    resources = concat(var.cles_sources_arns, [var.cle_kms_arn])

    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role_policy" "cles" {
  name   = "usage-des-cles"
  role   = aws_iam_role.sauvegarde.id
  policy = data.aws_iam_policy_document.cles.json
}

resource "aws_backup_vault" "this" {
  name        = "${var.prefixe}-coffre"
  kms_key_arn = var.cle_kms_arn

  tags = var.tags
}

resource "aws_backup_plan" "this" {
  name = "${var.prefixe}-plan-quotidien"

  rule {
    rule_name         = "quotidien"
    target_vault_name = aws_backup_vault.this.name
    schedule          = var.planification
    start_window      = 60
    completion_window = 360

    lifecycle {
      delete_after = var.retention_jours
    }
  }

  tags = var.tags
}

resource "aws_backup_selection" "bases" {
  name         = "bases-de-donnees"
  plan_id      = aws_backup_plan.this.id
  iam_role_arn = aws_iam_role.sauvegarde.arn
  resources    = var.ressources_arns
}
