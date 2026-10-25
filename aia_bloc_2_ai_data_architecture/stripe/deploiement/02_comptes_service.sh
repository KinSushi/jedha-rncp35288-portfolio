#!/bin/sh
# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
#
# Exécuté UNE fois par l'image PostgreSQL, à la création de la base, après le schéma
# (01_oltp_schema.sql). Crée les deux comptes de service qui se connectent réellement ; les rôles
# « groupe » (role_application, role_cdc) ont été créés par le schéma avec leurs droits.
# Les mots de passe viennent de fichiers de secrets (/run/secrets), jamais de ce dépôt.
# Le fichier est « sourcé » par le script d'entrée de l'image : pas de `set -u` ici.
set -e
MDP_APP=$(cat /run/secrets/postgres_app_password)
MDP_CDC=$(cat /run/secrets/postgres_cdc_password)
# Variables psql (:'nom') : la valeur est protégée comme un littéral SQL, sans injection possible.
psql -v ON_ERROR_STOP=1 -v mdp_app="$MDP_APP" -v mdp_cdc="$MDP_CDC" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE svc_application LOGIN PASSWORD :'mdp_app' IN ROLE role_application;
CREATE ROLE svc_debezium LOGIN PASSWORD :'mdp_cdc' IN ROLE role_cdc;
SQL
