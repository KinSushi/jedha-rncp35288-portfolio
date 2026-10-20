# Copyright © 2026 · voir le fichier LICENSE du dépôt
#
# Module « secrets » : les secrets applicatifs, dans AWS Secrets Manager.
#
# Règle : aucun secret n'est écrit dans le code, dans une variable d'environnement d'image ni dans
# un fichier de configuration versionné. Le code lit le secret à l'exécution, par son ARN, avec
# un rôle IAM qui n'a le droit de lire que ce secret (voir le module iam).
#
# Secrets créés ici :
# - `pseudonymisation/sel` : le sel du hachage SHA-256 des courriels des clients (RGPD,
#   pseudonymisation) ; la démonstration en utilise un sel fictif (stripe_demo/oltp.py, _hacher) ;
# - `cdc/postgresql` : l'identifiant et le mot de passe du compte de réplication `svc_debezium`
#   que Debezium utilise pour lire le journal de transactions.
# Les mots de passe administrateurs d'Aurora, de DocumentDB et de Redshift ne sont PAS ici : ces
# services créent et renouvellent eux-mêmes leur secret (manage_master_user_password).
#
# Les valeurs aléatoires sont produites par le fournisseur `random` : elles figurent donc, chiffrées
# et marquées sensibles, dans l'état Terraform. C'est pourquoi l'état est stocké dans un bucket S3
# chiffré, versionné et à accès restreint (voir terraform/README.md).

resource "random_password" "sel" {
  length  = 48
  special = false
}

resource "random_password" "cdc" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "sel" {
  #checkov:skip=CKV2_AWS_57:Le sel de pseudonymisation ne doit PAS tourner : changer sa valeur invaliderait toutes les empreintes de courriels deja stockees. Il est chiffre par KMS et lisible par un seul role.
  name                    = "${var.prefixe}/pseudonymisation/sel"
  description             = "Sel du hachage des courriels clients (pseudonymisation RGPD)"
  kms_key_id              = var.cle_kms_arn
  recovery_window_in_days = var.fenetre_recuperation_jours

  tags = var.tags
}

resource "aws_secretsmanager_secret_version" "sel" {
  secret_id     = aws_secretsmanager_secret.sel.id
  secret_string = random_password.sel.result
}

resource "aws_secretsmanager_secret" "cdc" {
  name                    = "${var.prefixe}/cdc/postgresql"
  description             = "Compte de replication Debezium sur Aurora PostgreSQL"
  kms_key_id              = var.cle_kms_arn
  recovery_window_in_days = var.fenetre_recuperation_jours

  tags = var.tags
}

resource "aws_secretsmanager_secret_version" "cdc" {
  secret_id = aws_secretsmanager_secret.cdc.id
  secret_string = jsonencode({
    username = "svc_debezium"
    password = random_password.cdc.result
  })
}

# Rotation automatique : elle exige une fonction Lambda de rotation (hors du périmètre de ce
# dépôt) dont l'ARN est fourni par l'environnement. Sans ARN, aucune rotation n'est planifiée :
# l'environnement de production DOIT en fournir un (validation dans environnements/prod).
resource "aws_secretsmanager_secret_rotation" "cdc" {
  count = var.fonction_rotation_arn == null ? 0 : 1

  secret_id           = aws_secretsmanager_secret.cdc.id
  rotation_lambda_arn = var.fonction_rotation_arn

  rotation_rules {
    automatically_after_days = var.rotation_jours
  }
}
