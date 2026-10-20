# Copyright © 2026 · voir le fichier LICENSE du dépôt

variable "prefixe" {
  description = "Préfixe des noms de ressources (projet et environnement)."
  type        = string
}

variable "region" {
  description = "Région AWS (destination des journaux des conteneurs)."
  type        = string
}

variable "sous_reseaux_ids" {
  description = "Sous-réseaux privés où placer les tâches (au moins deux zones de disponibilité)."
  type        = list(string)

  validation {
    condition     = length(var.sous_reseaux_ids) >= 2
    error_message = "Les tâches doivent pouvoir se répartir sur au moins deux zones de disponibilité."
  }
}

variable "groupe_securite_id" {
  description = "Groupe de sécurité « application » du module reseau : le seul autorisé à joindre les bases, le bus et l'entrepôt."
  type        = string
}

variable "cle_journaux_arn" {
  description = "ARN de la clé KMS des journaux (groupes CloudWatch des tâches et des sessions ECS Exec)."
  type        = string
}

variable "retention_journaux_jours" {
  description = "Durée de conservation des journaux des tâches (12 mois, comme les autres journaux)."
  type        = number
  default     = 365
}

variable "depots_ecr_arns" {
  description = "ARN des dépôts ECR d'où le rôle d'exécution peut tirer les images (aucun autre dépôt)."
  type        = list(string)

  validation {
    condition     = length(var.depots_ecr_arns) > 0 && alltrue([for arn in var.depots_ecr_arns : can(regex("^arn:aws:ecr:", arn))])
    error_message = "Fournir au moins un ARN de dépôt ECR (arn:aws:ecr:...)."
  }
}

variable "image_score" {
  description = "Image du service de score, désignée par son empreinte (…@sha256:…), jamais par une étiquette modifiable."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image_score))
    error_message = "L'image doit être désignée par son empreinte : <dépôt>@sha256:<64 caractères hexadécimaux>."
  }
}

variable "image_etl" {
  description = "Image des tâches d'ETL et de réentraînement, désignée par son empreinte (…@sha256:…)."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image_etl))
    error_message = "L'image doit être désignée par son empreinte : <dépôt>@sha256:<64 caractères hexadécimaux>."
  }
}

variable "role_score_arn" {
  description = "ARN du rôle de tâche du service de score (module iam)."
  type        = string
}

variable "role_etl_arn" {
  description = "ARN du rôle de tâche de l'ETL (module iam)."
  type        = string
}

variable "environnement_score" {
  description = "Variables d'environnement du service de score (adresses, noms) : JAMAIS de secret ici, les secrets se lisent dans Secrets Manager."
  type        = map(string)
  default     = {}
}

variable "environnement_etl" {
  description = "Variables d'environnement des tâches d'ETL (adresses, noms) : JAMAIS de secret ici."
  type        = map(string)
  default     = {}
}

variable "taches" {
  description = "Taille de chaque tâche Fargate : cpu en unités (1 024 = 1 vCPU), mémoire en Mio. HYPOTHÈSE H16 (document 09 § 4.5)."
  type = map(object({
    cpu     = number
    memoire = number
  }))

  # Combinaisons admises par Fargate (1 à 4 vCPU) : mémoire entre 2 et 8 fois le CPU, 30 720 Mio
  # au plus à 4 vCPU. AWS refuserait les autres à l'application ; l'erreur arrive ici plus tôt.
  validation {
    condition = alltrue([
      for t in values(var.taches) : contains([1024, 2048, 4096], t.cpu)
      && t.memoire >= 2 * t.cpu
      && t.memoire <= (t.cpu == 4096 ? 30720 : 8 * t.cpu)
    ])
    error_message = "Taille Fargate invalide : cpu parmi 1024, 2048, 4096 ; mémoire entre 2 et 8 fois le cpu (30 720 Mio au plus à 4096)."
  }

  validation {
    condition     = contains(keys(var.taches), "score") && contains(keys(var.taches), "etl")
    error_message = "Les tailles des tâches « score » et « etl » sont obligatoires."
  }
}

variable "score_taches_min" {
  description = "Nombre minimal de tâches du service de score (au moins 1). HYPOTHÈSE H16."
  type        = number

  validation {
    condition     = var.score_taches_min >= 1
    error_message = "Au moins une tâche de score doit tourner : sinon aucun paiement n'est noté."
  }
}

variable "score_taches_max" {
  description = "Nombre maximal de tâches du service de score : plafond de coût de l'autoscaling. HYPOTHÈSE H16."
  type        = number

  validation {
    condition     = var.score_taches_max >= 1
    error_message = "Le plafond de tâches de score doit valoir au moins 1."
  }
}

variable "score_cible_cpu_pourcent" {
  description = "Utilisation moyenne du CPU visée par l'autoscaling du score, en pour cent. HYPOTHÈSE H16."
  type        = number

  validation {
    condition     = var.score_cible_cpu_pourcent > 0 && var.score_cible_cpu_pourcent < 100
    error_message = "La cible de CPU doit être strictement comprise entre 0 et 100 %."
  }
}

variable "delai_montee_s" {
  description = "Attente minimale entre deux ajouts de tâches, en secondes. HYPOTHÈSE H16."
  type        = number
}

variable "delai_descente_s" {
  description = "Attente minimale entre deux retraits de tâches, en secondes (plus longue que la montée : on retire prudemment). HYPOTHÈSE H16."
  type        = number
}

variable "tags" {
  description = "Étiquettes supplémentaires."
  type        = map(string)
  default     = {}
}
