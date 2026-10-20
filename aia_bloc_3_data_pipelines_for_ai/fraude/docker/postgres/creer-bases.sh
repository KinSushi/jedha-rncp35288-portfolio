#!/bin/bash
# Copyright © 2026 · voir le fichier LICENSE du dépôt
# Exécuté une seule fois, à la première initialisation du volume PostgreSQL.
# Crée une base et un rôle par service (principe du moindre privilège) : un service compromis
# n'accède qu'à sa propre base. Les mots de passe viennent des secrets Docker, jamais du script.
set -euo pipefail

creer_base() {
    local base="$1" role="$2" fichier_secret="$3"
    local mot_de_passe
    mot_de_passe="$(cat "$fichier_secret")"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE ${role} LOGIN PASSWORD '${mot_de_passe}';
CREATE DATABASE ${base} OWNER ${role};
REVOKE ALL ON DATABASE ${base} FROM PUBLIC;
SQL
}

creer_base airflow airflow /run/secrets/pg_airflow_motdepasse
creer_base mlflow mlflow /run/secrets/pg_mlflow_motdepasse
creer_base fraude fraude /run/secrets/pg_fraude_motdepasse
