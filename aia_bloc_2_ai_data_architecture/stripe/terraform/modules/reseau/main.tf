# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
#
# Module « reseau » : un VPC entièrement PRIVÉ.
#
# Choix structurants :
# - aucune passerelle Internet, aucun NAT : rien dans ce VPC ne peut être joint depuis Internet ni
#   n'en joint un ; l'accès aux services AWS passe par des points de terminaison privés (VPC
#   endpoints), ce qui réduit aussi la surface d'attaque exigée par le périmètre PCI-DSS ;
# - sous-groupes de sécurité distincts par rôle (application, base transactionnelle, documents,
#   bus de messages, entrepôt) : seules les connexions nécessaires sont ouvertes, et uniquement
#   depuis le groupe « application » (segmentation) ;
# - journaux de flux réseau conservés 12 mois, chiffrés.

# Les zones sont NOMMÉES par l'appelant (variable `zones`) puis vérifiées auprès de la région :
# l'ensemble retenu est fermé et ne s'élargit pas si AWS ajoute une zone de disponibilité.
data "aws_availability_zones" "disponibles" {
  state = "available"

  filter {
    name   = "zone-name"
    values = var.zones
  }
}

locals {
  zones = data.aws_availability_zones.disponibles.names
}

resource "aws_vpc" "principal" {
  cidr_block           = var.cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = "${var.prefixe}-vpc" })
}

# Le groupe de sécurité par défaut du VPC est vidé de toute règle : rien ne doit l'utiliser.
resource "aws_default_security_group" "defaut" {
  vpc_id = aws_vpc.principal.id

  tags = merge(var.tags, { Name = "${var.prefixe}-sg-defaut-ferme" })
}

# ------------------------------------------------------------------ sous-réseaux privés

resource "aws_subnet" "prive" {
  count = length(local.zones)

  vpc_id                  = aws_vpc.principal.id
  availability_zone       = local.zones[count.index]
  cidr_block              = cidrsubnet(var.cidr, 4, count.index)
  map_public_ip_on_launch = false

  tags = merge(var.tags, { Name = "${var.prefixe}-prive-${local.zones[count.index]}" })
}

# Table de routage sans route par défaut : seul le trafic interne au VPC est routé.
resource "aws_route_table" "prive" {
  vpc_id = aws_vpc.principal.id

  tags = merge(var.tags, { Name = "${var.prefixe}-routes-privees" })
}

resource "aws_route_table_association" "prive" {
  count = length(local.zones)

  subnet_id      = aws_subnet.prive[count.index].id
  route_table_id = aws_route_table.prive.id
}

# ------------------------------------------------------------------ journaux de flux

resource "aws_cloudwatch_log_group" "flux_vpc" {
  name              = "/vpc/${var.prefixe}/flux"
  retention_in_days = var.retention_journaux_jours
  kms_key_id        = var.cle_journaux_arn

  tags = var.tags
}

data "aws_iam_policy_document" "flux_vpc_confiance" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "flux_vpc_droits" {
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.flux_vpc.arn}:*"]
  }
}

resource "aws_iam_role" "flux_vpc" {
  name               = "${var.prefixe}-flux-vpc"
  assume_role_policy = data.aws_iam_policy_document.flux_vpc_confiance.json

  tags = var.tags
}

resource "aws_iam_role_policy" "flux_vpc" {
  name   = "ecriture-journaux"
  role   = aws_iam_role.flux_vpc.id
  policy = data.aws_iam_policy_document.flux_vpc_droits.json
}

resource "aws_flow_log" "vpc" {
  vpc_id          = aws_vpc.principal.id
  traffic_type    = "ALL"
  iam_role_arn    = aws_iam_role.flux_vpc.arn
  log_destination = aws_cloudwatch_log_group.flux_vpc.arn

  tags = var.tags
}

# ------------------------------------------------------------------ groupes de sécurité

resource "aws_security_group" "application" {
  #checkov:skip=CKV2_AWS_5:Groupe rattache a ses ressources dans d'autres modules (variable groupe_securite_id) ou par les equipes plateforme (taches ECS, connecteurs) : le controle, limite a ce module, ne voit pas le rattachement.
  name        = "${var.prefixe}-application"
  description = "Charges applicatives (ETL, connecteurs, services) autorisees a joindre les donnees"
  vpc_id      = aws_vpc.principal.id

  tags = merge(var.tags, { Name = "${var.prefixe}-sg-application" })
}

resource "aws_security_group" "points_terminaison" {
  name        = "${var.prefixe}-points-terminaison"
  description = "Points de terminaison prives vers les services AWS (HTTPS depuis le VPC)"
  vpc_id      = aws_vpc.principal.id

  tags = merge(var.tags, { Name = "${var.prefixe}-sg-points-terminaison" })
}

resource "aws_security_group" "base_transactionnelle" {
  #checkov:skip=CKV2_AWS_5:Groupe rattache a ses ressources dans d'autres modules (variable groupe_securite_id) ou par les equipes plateforme (taches ECS, connecteurs) : le controle, limite a ce module, ne voit pas le rattachement.
  name        = "${var.prefixe}-aurora"
  description = "Aurora PostgreSQL : accessible uniquement depuis le groupe application"
  vpc_id      = aws_vpc.principal.id

  tags = merge(var.tags, { Name = "${var.prefixe}-sg-aurora" })
}

resource "aws_security_group" "documents" {
  #checkov:skip=CKV2_AWS_5:Groupe rattache a ses ressources dans d'autres modules (variable groupe_securite_id) ou par les equipes plateforme (taches ECS, connecteurs) : le controle, limite a ce module, ne voit pas le rattachement.
  name        = "${var.prefixe}-documentdb"
  description = "DocumentDB : accessible uniquement depuis le groupe application"
  vpc_id      = aws_vpc.principal.id

  tags = merge(var.tags, { Name = "${var.prefixe}-sg-documentdb" })
}

resource "aws_security_group" "bus" {
  #checkov:skip=CKV2_AWS_5:Groupe rattache a ses ressources dans d'autres modules (variable groupe_securite_id) ou par les equipes plateforme (taches ECS, connecteurs) : le controle, limite a ce module, ne voit pas le rattachement.
  name        = "${var.prefixe}-msk"
  description = "MSK Serverless : accessible uniquement depuis le groupe application"
  vpc_id      = aws_vpc.principal.id

  tags = merge(var.tags, { Name = "${var.prefixe}-sg-msk" })
}

resource "aws_security_group" "entrepot" {
  #checkov:skip=CKV2_AWS_5:Groupe rattache a ses ressources dans d'autres modules (variable groupe_securite_id) ou par les equipes plateforme (taches ECS, connecteurs) : le controle, limite a ce module, ne voit pas le rattachement.
  name        = "${var.prefixe}-redshift"
  description = "Redshift Serverless : accessible uniquement depuis le groupe application"
  vpc_id      = aws_vpc.principal.id

  tags = merge(var.tags, { Name = "${var.prefixe}-sg-redshift" })
}

# Règles d'entrée : un port, une source (le groupe application), une raison.
locals {
  acces_depuis_application = {
    base_transactionnelle = { groupe = aws_security_group.base_transactionnelle.id, port = 5432, usage = "PostgreSQL" }
    documents             = { groupe = aws_security_group.documents.id, port = 27017, usage = "MongoDB API" }
    bus                   = { groupe = aws_security_group.bus.id, port = 9098, usage = "Kafka SASL IAM" }
    entrepot              = { groupe = aws_security_group.entrepot.id, port = 5439, usage = "Redshift" }
  }
}

resource "aws_vpc_security_group_ingress_rule" "depuis_application" {
  for_each = local.acces_depuis_application

  security_group_id            = each.value.groupe
  referenced_security_group_id = aws_security_group.application.id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  description                  = "${each.value.usage} depuis le groupe application"
}

resource "aws_vpc_security_group_ingress_rule" "points_terminaison_https" {
  security_group_id = aws_security_group.points_terminaison.id
  cidr_ipv4         = var.cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "HTTPS depuis le VPC"
}

# Sorties : le groupe application ne peut joindre que les ports des données et les points de
# terminaison ; les autres groupes n'ont aucune sortie (ils ne répondent qu'aux connexions reçues).
resource "aws_vpc_security_group_egress_rule" "application_vers_donnees" {
  for_each = local.acces_depuis_application

  security_group_id            = aws_security_group.application.id
  referenced_security_group_id = each.value.groupe
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  description                  = "Vers ${each.value.usage}"
}

# S3 par le point de terminaison de type passerelle : la sortie vise la liste de préfixes de S3
# (et non 0.0.0.0/0). Les tâches de calcul en ont besoin pour le lac et pour les couches des
# images, que le registre ECR stocke dans S3.
resource "aws_vpc_security_group_egress_rule" "application_vers_s3" {
  security_group_id = aws_security_group.application.id
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "Vers S3 par le point de terminaison passerelle (HTTPS)"
}

resource "aws_vpc_security_group_egress_rule" "application_vers_points_terminaison" {
  security_group_id            = aws_security_group.application.id
  referenced_security_group_id = aws_security_group.points_terminaison.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "Vers les points de terminaison prives (HTTPS)"
}

# ------------------------------------------------------------------ points de terminaison

# S3 : point de terminaison de type passerelle (gratuit), ajouté à la table de routage privée.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.principal.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.prive.id]

  tags = merge(var.tags, { Name = "${var.prefixe}-ep-s3" })
}

# Autres services : points de terminaison d'interface, avec DNS privé.
resource "aws_vpc_endpoint" "interface" {
  for_each = toset(var.services_points_terminaison)

  vpc_id              = aws_vpc.principal.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.prive[*].id
  security_group_ids  = [aws_security_group.points_terminaison.id]
  private_dns_enabled = true

  tags = merge(var.tags, { Name = "${var.prefixe}-ep-${each.key}" })
}
