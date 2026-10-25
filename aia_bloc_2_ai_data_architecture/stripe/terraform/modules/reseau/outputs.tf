# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC

output "vpc_id" {
  description = "Identifiant du VPC."
  value       = aws_vpc.principal.id
}

output "vpc_cidr" {
  description = "Plage d'adresses du VPC."
  value       = aws_vpc.principal.cidr_block
}

output "sous_reseaux_prives" {
  description = "Identifiants des sous-réseaux privés, un par zone."
  value       = aws_subnet.prive[*].id
}

output "zones" {
  description = "Zones de disponibilité utilisées, dans l'ordre des sous-réseaux."
  value       = local.zones
}

output "groupes_securite" {
  description = "Identifiants des groupes de sécurité, par rôle."
  value = {
    application           = aws_security_group.application.id
    base_transactionnelle = aws_security_group.base_transactionnelle.id
    documents             = aws_security_group.documents.id
    bus                   = aws_security_group.bus.id
    entrepot              = aws_security_group.entrepot.id
  }
}
