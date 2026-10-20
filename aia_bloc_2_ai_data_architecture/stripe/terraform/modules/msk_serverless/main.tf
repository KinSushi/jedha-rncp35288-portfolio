# Copyright © 2026 · voir le fichier LICENSE du dépôt
#
# Module « msk_serverless » : le bus de messages (API Kafka), Amazon MSK Serverless.
#
# Il transporte les changements de la base transactionnelle publiés par Debezium (un sujet par
# table : stripe.public.paiements, etc.) vers l'entrepôt, le magasin de caractéristiques et le
# service de score de fraude.
#
# Pourquoi « Serverless » : pas de courtiers à dimensionner ni à patcher (le service les gère),
# capacité qui suit le débit, facturation à l'usage ; coût : moins de réglages fins qu'un
# cluster MSK provisionné (voir la note d'arbitrage, 09_dimensionnement_finops_greenops.md).
#
# Sécurité : accès uniquement depuis le VPC (sous-réseaux privés + groupe de sécurité dédié),
# authentification et autorisation par IAM (SASL/IAM, aucun mot de passe Kafka), chiffrement
# des données en transit (TLS) et au repos assurés par le service. MSK Serverless ne propose pas
# de clé KMS du client : le chiffrement au repos y utilise des clés gérées par AWS.

resource "aws_msk_serverless_cluster" "this" {
  cluster_name = "${var.prefixe}-msk"

  vpc_config {
    subnet_ids         = var.sous_reseaux_ids
    security_group_ids = [var.groupe_securite_id]
  }

  client_authentication {
    sasl {
      iam {
        enabled = true
      }
    }
  }

  tags = var.tags
}
