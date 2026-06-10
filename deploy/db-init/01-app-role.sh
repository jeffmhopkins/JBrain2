#!/bin/bash
# Runs once at first database init. The app role is unprivileged on purpose:
# RLS policies bind it, and migrations (running as the superuser) own the DDL.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE jbrain_app LOGIN PASSWORD '${APP_DB_PASSWORD}';
EOSQL
