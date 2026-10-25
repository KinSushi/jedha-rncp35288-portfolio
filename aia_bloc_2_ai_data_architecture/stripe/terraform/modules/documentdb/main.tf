# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
#
# Module « documentdb » : la base de documents (NoSQL), Amazon DocumentDB compatible MongoDB.
#
# Elle porte les quatre collections du dossier stripe/nosql : événements de navigation, retours
# clients, caractéristiques en ligne du score de fraude, signaux de fraude.
#
# Sécurité : chiffrement au repos (clé KMS du domaine « documents »), TLS imposé par le groupe
# de paramètres, mot de passe administrateur géré par Secrets Manager (jamais dans le code),
# journaux d'audit et de profilage exportés vers CloudWatch, aucune adresse publique.
# Disponibilité : une instance en écriture + des réplicas dans d'autres zones ; en cas de panne,
# un réplica est promu selon sa priorité (`promotion_tier`).

resource "aws_docdb_subnet_group" "this" {
  name       = "${var.prefixe}-documentdb"
  subnet_ids = var.sous_reseaux_ids

  tags = var.tags
}

resource "aws_docdb_cluster_parameter_group" "this" {
  name        = "${var.prefixe}-docdb"
  family      = var.famille
  description = "Parametres DocumentDB : TLS impose, audit actif"

  parameter {
    name  = "tls"
    value = "enabled"
  }

  parameter {
    name  = "audit_logs"
    value = "enabled"
  }

  # Les documents « evenements » portent un index TTL (90 jours) : le moniteur TTL doit rester actif.
  parameter {
    name  = "ttl_monitor"
    value = "enabled"
  }

  tags = var.tags
}

# Groupes de journaux créés ici, avant le cluster, pour imposer chiffrement et rétention.
resource "aws_cloudwatch_log_group" "journaux" {
  for_each = toset(["audit", "profiler"])

  name              = "/aws/docdb/${var.prefixe}-documentdb/${each.key}"
  retention_in_days = var.retention_journaux_jours
  kms_key_id        = var.cle_journaux_arn

  tags = var.tags
}

resource "aws_docdb_cluster" "this" {
  cluster_identifier = "${var.prefixe}-documentdb"
  engine             = "docdb"
  engine_version     = var.version_moteur
  port               = 27017

  master_username = var.administrateur
  # Mot de passe créé et conservé par Secrets Manager : il n'apparaît ni dans le code ni dans
  # l'état Terraform. (Ce secret est chiffré par la clé gérée par AWS pour Secrets Manager :
  # le service ne propose pas ici de clé du client.)
  manage_master_user_password = true

  storage_encrypted = true
  kms_key_id        = var.cle_kms_arn

  db_subnet_group_name            = aws_docdb_subnet_group.this.name
  vpc_security_group_ids          = [var.groupe_securite_id]
  db_cluster_parameter_group_name = aws_docdb_cluster_parameter_group.this.name

  backup_retention_period      = var.retention_sauvegardes_jours
  preferred_backup_window      = var.fenetre_sauvegarde
  preferred_maintenance_window = var.fenetre_maintenance

  deletion_protection       = var.protection_suppression
  skip_final_snapshot       = !var.protection_suppression
  final_snapshot_identifier = var.protection_suppression ? "${var.prefixe}-documentdb-final" : null

  enabled_cloudwatch_logs_exports = ["audit", "profiler"]

  tags = var.tags

  depends_on = [aws_cloudwatch_log_group.journaux]
}

resource "aws_docdb_cluster_instance" "this" {
  count = var.nb_instances

  identifier         = "${var.prefixe}-documentdb-${count.index + 1}"
  cluster_identifier = aws_docdb_cluster.this.id
  instance_class     = var.classe_instance

  availability_zone = var.zones[count.index % length(var.zones)]
  promotion_tier    = count.index

  auto_minor_version_upgrade      = true
  copy_tags_to_snapshot           = true
  enable_performance_insights     = true
  performance_insights_kms_key_id = var.cle_kms_arn

  tags = var.tags
}
