-- Run once as a PostgreSQL 17 cluster superuser with protected environment passwords.
-- This production bootstrap intentionally never creates test roles or test databases.
\getenv database_owner_password FIREDASH_DATABASE_OWNER_PASSWORD
\getenv application_runtime_password FIREDASH_APPLICATION_RUNTIME_PASSWORD
\getenv backup_role_password FIREDASH_BACKUP_ROLE_PASSWORD
\if :{?database_owner_password}
\else
\quit 3
\endif
\if :{?application_runtime_password}
\else
\quit 3
\endif
\if :{?backup_role_password}
\else
\quit 3
\endif

SELECT format(
    'CREATE ROLE database_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'database_owner_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'database_owner')
\gexec

SELECT format(
    'CREATE ROLE application_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'application_runtime_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'application_runtime')
\gexec

SELECT format(
    'CREATE ROLE backup_role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'backup_role_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'backup_role')
\gexec

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname IN ('database_owner', 'application_runtime', 'backup_role')
          AND (NOT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'FireDash production roles have unsafe attributes';
    END IF;
END $$;

SELECT 'CREATE DATABASE fire_backend OWNER database_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'fire_backend')
\gexec

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_database database
        JOIN pg_roles owner ON owner.oid = database.datdba
        WHERE database.datname = 'fire_backend' AND owner.rolname = 'database_owner'
    ) THEN
        RAISE EXCEPTION 'fire_backend must be owned by database_owner';
    END IF;
END $$;

\connect fire_backend
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS btree_gist;

REVOKE CONNECT, TEMPORARY ON DATABASE fire_backend FROM PUBLIC;
GRANT CONNECT ON DATABASE fire_backend TO application_runtime, backup_role;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO application_runtime, backup_role;
