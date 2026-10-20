# Copyright © 2026 · voir le fichier LICENSE du dépôt
#
# Module « aurora_postgresql » : la base transactionnelle (OLTP), Aurora PostgreSQL 16.
#
# Disponibilité : le stockage Aurora est répliqué sur trois zones ; les instances (une en
# écriture, une ou plusieurs en lecture) sont placées dans des zones DIFFÉRENTES. Si la zone de
# l'instance en écriture tombe, Aurora promeut un lecteur (priorité `promotion_tier`) et le
# point de terminaison du cluster pointe vers lui.
#
# Sécurité : chiffrement au repos (clé KMS du domaine « oltp »), TLS imposé (rds.force_ssl),
# authentification IAM activée, mot de passe administrateur généré ET stocké par Secrets Manager
# (jamais dans le code ni dans l'état Terraform), journaux PostgreSQL et audit (pgaudit) exportés
# vers CloudWatch, aucune adresse publique, protection contre la suppression.
#
# Capture des changements : `rds.logical_replication = 1` active le décodage logique du journal
# de transactions, prérequis de Debezium (voir stripe/deploiement/debezium_connecteur.json).

resource "aws_db_subnet_group" "this" {
  name       = "${var.prefixe}-aurora"
  subnet_ids = var.sous_reseaux_ids

  tags = var.tags
}

resource "aws_rds_cluster_parameter_group" "this" {
  name        = "${var.prefixe}-aurora-pg16"
  family      = "aurora-postgresql16"
  description = "Parametres Aurora PostgreSQL 16 : TLS impose, decodage logique, audit"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  # Paramètre statique : pris en compte au prochain redémarrage des instances.
  parameter {
    name         = "rds.logical_replication"
    value        = "1"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "shared_preload_libraries"
    value        = "pgaudit,pg_stat_statements"
    apply_method = "pending-reboot"
  }

  # Audit : définitions de schéma et gestion des rôles (qui a changé quoi, quand).
  parameter {
    name  = "pgaudit.log"
    value = "ddl,role"
  }

  parameter {
    name  = "log_statement"
    value = "ddl"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = tostring(var.seuil_requete_lente_ms)
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  tags = var.tags
}

# Le groupe de journaux est créé ici, avant le cluster : sinon RDS le crée lui-même, sans
# chiffrement ni durée de rétention. 365 jours : PCI-DSS v4.0.1, exigence 10.5.1.
resource "aws_cloudwatch_log_group" "postgresql" {
  name              = "/aws/rds/cluster/${var.prefixe}-aurora/postgresql"
  retention_in_days = var.retention_journaux_jours
  kms_key_id        = var.cle_journaux_arn

  tags = var.tags
}

resource "aws_rds_cluster" "this" {
  #checkov:skip=CKV2_AWS_8:Le plan AWS Backup est defini dans le module sauvegarde, qui recoit l'ARN du cluster en entree ; le controle ne suit pas les references entre modules.
  #checkov:skip=CKV_AWS_139:La protection contre la suppression est un parametre (vraie par defaut et en prod, verifiee par un bloc check dans environnements/prod) ; seul l'environnement dev la leve pour pouvoir detruire et recreer.
  cluster_identifier = "${var.prefixe}-aurora"
  engine             = "aurora-postgresql"
  engine_version     = var.version_moteur
  database_name      = var.nom_base
  master_username    = var.administrateur

  # Le mot de passe administrateur est créé et renouvelé par Secrets Manager.
  manage_master_user_password   = true
  master_user_secret_kms_key_id = var.cle_secrets_arn

  storage_encrypted                   = true
  kms_key_id                          = var.cle_kms_arn
  iam_database_authentication_enabled = true
  copy_tags_to_snapshot               = true

  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [var.groupe_securite_id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name

  backup_retention_period      = var.retention_sauvegardes_jours
  preferred_backup_window      = var.fenetre_sauvegarde
  preferred_maintenance_window = var.fenetre_maintenance

  deletion_protection       = var.protection_suppression
  skip_final_snapshot       = !var.protection_suppression
  final_snapshot_identifier = var.protection_suppression ? "${var.prefixe}-aurora-final" : null

  enabled_cloudwatch_logs_exports = ["postgresql"]

  tags = var.tags

  depends_on = [aws_cloudwatch_log_group.postgresql]
}

# ------------------------------------------------------------------ supervision détaillée

data "aws_iam_policy_document" "surveillance_confiance" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "surveillance" {
  name               = "${var.prefixe}-aurora-surveillance"
  assume_role_policy = data.aws_iam_policy_document.surveillance_confiance.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "surveillance" {
  role       = aws_iam_role.surveillance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

# ------------------------------------------------------------------ instances

resource "aws_rds_cluster_instance" "this" {
  count = var.nb_instances

  identifier         = "${var.prefixe}-aurora-${count.index + 1}"
  cluster_identifier = aws_rds_cluster.this.id
  engine             = aws_rds_cluster.this.engine
  engine_version     = aws_rds_cluster.this.engine_version
  instance_class     = var.classe_instance

  # Une instance par zone, dans l'ordre : l'instance 1 est le premier choix d'écriture.
  availability_zone = var.zones[count.index % length(var.zones)]
  promotion_tier    = count.index

  publicly_accessible        = false
  auto_minor_version_upgrade = true
  copy_tags_to_snapshot      = true

  performance_insights_enabled    = true
  performance_insights_kms_key_id = var.cle_kms_arn
  monitoring_interval             = 60
  monitoring_role_arn             = aws_iam_role.surveillance.arn

  tags = var.tags
}
