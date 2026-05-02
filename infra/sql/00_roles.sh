#!/usr/bin/env bash
# Phase 3b: Postgres role bootstrap.
#
# Runs as the postgres superuser via /docker-entrypoint-initdb.d on first
# container start. Idempotent: a re-run is a no-op (DO-block guards on
# pg_roles).
#
# Container env supplies app-role passwords (PGPASSWORD_*); they are
# interpolated by the shell before psql sees the SQL, which avoids psql
# variable-substitution limitations inside DO-blocks.

set -euo pipefail

: "${POSTGRES_USER:?POSTGRES_USER must be set}"
: "${POSTGRES_DB:?POSTGRES_DB must be set}"
: "${PGPASSWORD_TRADING_CYCLE:?PGPASSWORD_TRADING_CYCLE must be set}"
: "${PGPASSWORD_OUTCOME_INGESTION:?PGPASSWORD_OUTCOME_INGESTION must be set}"
: "${PGPASSWORD_LESSONS_SUMMARY:?PGPASSWORD_LESSONS_SUMMARY must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'trading_cycle') THEN
        EXECUTE format('CREATE ROLE trading_cycle LOGIN PASSWORD %L', '${PGPASSWORD_TRADING_CYCLE}');
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'outcome_ingestion') THEN
        EXECUTE format('CREATE ROLE outcome_ingestion LOGIN PASSWORD %L', '${PGPASSWORD_OUTCOME_INGESTION}');
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'lessons_summary') THEN
        EXECUTE format('CREATE ROLE lessons_summary LOGIN PASSWORD %L', '${PGPASSWORD_LESSONS_SUMMARY}');
    END IF;
END
\$\$;

GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO trading_cycle, outcome_ingestion, lessons_summary;
GRANT USAGE ON SCHEMA public TO trading_cycle, outcome_ingestion, lessons_summary;
EOSQL
