# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
#
# Module « lac_s3 » : le lac de données et ses deux buckets d'accompagnement.
#
# - `lac` : fichiers Parquet des micro-lots CDC et des extractions batch ; l'entrepôt (COPY) et
#   les entraînements du modèle de fraude y lisent. Chiffré avec la clé KMS du domaine « lac ».
# - `journaux` : journaux d'accès du lac. Chiffré en SSE-S3, car le service de journalisation
#   d'accès S3 ne sait pas écrire dans un bucket chiffré par une clé KMS du client (limite
#   documentée du service, à reverifier dans la documentation AWS en vigueur).
# - `audit` : archive immuable (verrouillage d'objets) où l'on exporte le journal d'audit de la
#   base (table journal_audit) : preuve d'audit protégée contre toute modification, PCI-DSS v4.0.1
#   exigences 10.3 (protection) et 10.5.1 (12 mois d'historique).
# Tous trois reprennent les garanties du module `bucket_s3`.

data "aws_caller_identity" "courant" {}

locals {
  compte      = data.aws_caller_identity.courant.account_id
  nom_lac     = "${var.prefixe}-lac-${local.compte}"
  nom_journal = "${var.prefixe}-journaux-${local.compte}"
  nom_audit   = "${var.prefixe}-audit-${local.compte}"
}

# Seul le service de journalisation S3, pour les buckets du lac et de l'archive d'audit de ce
# compte, peut écrire dans le bucket de journaux. Les ARN se calculent depuis les noms : pas de
# dépendance circulaire entre les modules.
data "aws_iam_policy_document" "ecriture_journaux" {
  statement {
    sid       = "EcritureParLeServiceDeJournalisation"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["arn:aws:s3:::${local.nom_journal}/*"]

    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:s3:::${local.nom_lac}", "arn:aws:s3:::${local.nom_audit}"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.compte]
    }
  }
}

module "journaux" {
  source = "../bucket_s3"

  nom                          = local.nom_journal
  cle_kms_arn                  = null
  autoriser_destruction        = var.autoriser_destruction
  politique_additionnelle_json = data.aws_iam_policy_document.ecriture_journaux.json
  transitions                  = [{ jours = var.transition_froid_jours, classe = "GLACIER" }]
  versions_anciennes_jours     = var.retention_audit_jours
  tags                         = merge(var.tags, { role = "journaux-acces" })
}

module "lac" {
  source = "../bucket_s3"

  nom                   = local.nom_lac
  cle_kms_arn           = var.cle_lac_arn
  bucket_journaux_id    = module.journaux.id
  autoriser_destruction = var.autoriser_destruction
  # Les fichiers de plus de N jours sont relus rarement (réentraînement, audit) : classe
  # « accès peu fréquent », puis « archivage ». Les seuils sont des HYPOTHÈSES de départ à
  # recaler sur les statistiques d'accès réelles (S3 Storage Lens).
  transitions = [
    { jours = var.transition_peu_frequent_jours, classe = "STANDARD_IA" },
    { jours = var.transition_froid_jours, classe = "GLACIER" },
  ]
  versions_anciennes_jours = var.versions_anciennes_jours
  tags                     = merge(var.tags, { role = "lac-de-donnees" })
}

module "audit" {
  source = "../bucket_s3"

  nom                      = local.nom_audit
  cle_kms_arn              = var.cle_audit_arn
  bucket_journaux_id       = module.journaux.id
  verrouillage_jours       = var.retention_audit_jours
  autoriser_destruction    = false
  transitions              = [{ jours = var.transition_froid_jours, classe = "GLACIER" }]
  versions_anciennes_jours = var.retention_audit_jours
  tags                     = merge(var.tags, { role = "archive-audit" })
}
