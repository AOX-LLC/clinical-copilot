#!/usr/bin/env bash
# Runs once, on first initialisation of the data directory (postgres entrypoint).
#
# Two roles by design: the owner role (POSTGRES_USER) runs the migrations and
# owns every object. The application role created here can log in and connect,
# and receives only what the migrations explicitly GRANT to it. Table-level
# privileges are NOT granted here.
set -euo pipefail

: "${APP_DB_PASSWORD:?APP_DB_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 \
     -v app_password="$APP_DB_PASSWORD" \
     -v dbname="$POSTGRES_DB" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE copilot_app LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

GRANT CONNECT ON DATABASE :"dbname" TO copilot_app;
GRANT USAGE ON SCHEMA public TO copilot_app;
SQL
