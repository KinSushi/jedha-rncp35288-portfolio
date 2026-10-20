# Copyright © 2026 · voir le fichier LICENSE du dépôt
#
# Module « redshift_serverless » : l'entrepôt analytique (OLAP), Amazon Redshift Serverless.
#
# Il héberge l'étoile décrite dans stripe/sql/olap_schema.sql. Le « namespace » porte les données,
# les utilisateurs et le chiffrement ; le « workgroup » porte le calcul (capacité en RPU) et le
# réseau. Sécurité : chiffrement avec la clé KMS du domaine « analytique », mot de passe
# administrateur géré par Secrets Manager, TLS obligatoire, routage VPC amélioré (le trafic
# COPY/UNLOAD vers S3 reste dans le réseau AWS, jamais sur Internet), pas d'accès public,
# journaux de connexion et d'activité exportés.
# Maîtrise des coûts : plafond mensuel de consommation (usage limit) et durée maximale de requête.

resource "aws_redshiftserverless_namespace" "this" {
  namespace_name = "${var.prefixe}-dwh"
  db_name        = var.nom_base

  admin_username                   = var.administrateur
  manage_admin_password            = true
  admin_password_secret_kms_key_id = var.cle_secrets_arn

  kms_key_id           = var.cle_kms_arn
  iam_roles            = [var.role_s3_arn]
  default_iam_role_arn = var.role_s3_arn

  log_exports = ["userlog", "connectionlog", "useractivitylog"]

  tags = var.tags
}

resource "aws_redshiftserverless_workgroup" "this" {
  namespace_name = aws_redshiftserverless_namespace.this.namespace_name
  workgroup_name = "${var.prefixe}-dwh"

  base_capacity = var.capacite_base_rpu
  max_capacity  = var.capacite_max_rpu

  enhanced_vpc_routing = true
  publicly_accessible  = false
  subnet_ids           = var.sous_reseaux_ids
  security_group_ids   = [var.groupe_securite_id]

  config_parameter {
    parameter_key   = "require_ssl"
    parameter_value = "true"
  }

  config_parameter {
    parameter_key   = "enable_user_activity_logging"
    parameter_value = "true"
  }

  # Une requête qui dépasse cette durée est interrompue : garde-fou contre les requêtes folles.
  config_parameter {
    parameter_key   = "max_query_execution_time"
    parameter_value = tostring(var.duree_max_requete_s)
  }

  tags = var.tags
}

# Plafond de consommation : au-delà, le service journalise et émet une métrique (alerte), ou
# désactive le calcul si `action_depassement = "deactivate"`.
resource "aws_redshiftserverless_usage_limit" "calcul" {
  resource_arn  = aws_redshiftserverless_workgroup.this.arn
  usage_type    = "serverless-compute"
  period        = "monthly"
  amount        = var.plafond_rpu_heures_mensuel
  breach_action = var.action_depassement
}
