-- Run once as a PostgreSQL superuser. Substitute strong passwords outside source control.
CREATE ROLE database_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD 'replace-me';
CREATE ROLE application_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD 'replace-me';
CREATE ROLE backup_role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD 'replace-me';

CREATE DATABASE fire_backend OWNER database_owner;
\connect fire_backend
CREATE EXTENSION postgis;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO application_runtime;
GRANT CONNECT ON DATABASE fire_backend TO application_runtime, backup_role;
GRANT TEMPORARY ON DATABASE fire_backend TO application_runtime;
