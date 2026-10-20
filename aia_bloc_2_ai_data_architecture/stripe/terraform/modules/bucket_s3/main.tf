# Copyright © 2026 · voir le fichier LICENSE du dépôt
#
# Module « bucket_s3 » : un bucket S3 DURCI, réutilisé pour le lac, les journaux d'accès et
# l'archive d'audit. Un seul endroit définit donc les garanties de base :
# - aucun accès public possible (quatre protections activées, propriété forcée au propriétaire) ;
# - chiffrement au repos : SSE-KMS (clé du client, clé de bucket activée) ou, quand la clé du
#   client n'est pas utilisable par le service écrivain, SSE-S3 ;
# - versionnement : une suppression ou une écriture erronée se corrige ;
# - TLS obligatoire : toute requête non chiffrée est refusée par la politique du bucket ;
# - cycle de vie : bascule vers des classes moins chères, purge des versions anciennes,
#   abandon des téléversements multipartie inachevés ;
# - événements publiés sur EventBridge (déclenchement de traitements sans interrogation) ;
# - journalisation d'accès et verrouillage d'objets (rétention) en option.

resource "aws_s3_bucket" "this" {
  #checkov:skip=CKV_AWS_144:Replication inter-regions non retenue dans ce perimetre : donnees maintenues dans une seule region (souverainete), durabilite multi-zones de S3, versionnement et verrouillage d'objets contre la suppression. A reevaluer si l'objectif de reprise apres sinistre regional l'exige (voir 10_continuite_supervision_maintenance.md).
  bucket              = var.nom
  force_destroy       = var.autoriser_destruction
  object_lock_enabled = var.verrouillage_jours != null

  tags = var.tags
}

resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.cle_kms_arn == null ? "AES256" : "aws:kms"
      kms_master_key_id = var.cle_kms_arn
    }
    bucket_key_enabled = var.cle_kms_arn == null ? null : true
  }
}

# Verrouillage des objets (WORM) : pendant la durée de rétention, un objet ne peut être ni modifié
# ni supprimé. Mode GOUVERNANCE : seul un rôle explicitement autorisé peut lever le verrou
# (le mode COMPLIANCE, irréversible, serait à choisir après validation par l'évaluateur).
resource "aws_s3_bucket_object_lock_configuration" "this" {
  count = var.verrouillage_jours == null ? 0 : 1

  bucket = aws_s3_bucket.this.id

  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.verrouillage_jours
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_logging" "this" {
  count = var.bucket_journaux_id == null ? 0 : 1

  bucket        = aws_s3_bucket.this.id
  target_bucket = var.bucket_journaux_id
  target_prefix = "${var.nom}/"
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    id     = "cycle-de-vie"
    status = "Enabled"

    filter {}

    dynamic "transition" {
      for_each = var.transitions

      content {
        days          = transition.value.jours
        storage_class = transition.value.classe
      }
    }

    noncurrent_version_expiration {
      noncurrent_days = var.versions_anciennes_jours
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_notification" "this" {
  bucket      = aws_s3_bucket.this.id
  eventbridge = true
}

data "aws_iam_policy_document" "this" {
  source_policy_documents = var.politique_additionnelle_json == null ? [] : [var.politique_additionnelle_json]

  statement {
    sid       = "RefuserLeNonChiffre"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  bucket = aws_s3_bucket.this.id
  policy = data.aws_iam_policy_document.this.json

  depends_on = [aws_s3_bucket_public_access_block.this]
}
