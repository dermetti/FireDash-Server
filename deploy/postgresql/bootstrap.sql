-- Run once as a PostgreSQL superuser. Substitute strong passwords outside source control.
CREATE ROLE database_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD 'replace-me';
CREATE ROLE application_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD 'replace-me';
CREATE ROLE backup_role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD 'replace-me';
-- This role is only for Django tests. It can create disposable databases from the template below.
CREATE ROLE firedash_test LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD 'replace-me';

CREATE DATABASE fire_backend OWNER database_owner;
\connect fire_backend
CREATE EXTENSION postgis;

-- Django's test database is cloned from this PostGIS-enabled, non-connectable template.
CREATE DATABASE firedash_test_template OWNER database_owner TEMPLATE template0;
\connect firedash_test_template
CREATE EXTENSION postgis;
\connect fire_backend
ALTER DATABASE firedash_test_template IS_TEMPLATE true;
ALTER DATABASE firedash_test_template ALLOW_CONNECTIONS false;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO application_runtime;
GRANT CONNECT ON DATABASE fire_backend TO application_runtime, backup_role;
GRANT TEMPORARY ON DATABASE fire_backend TO application_runtime;
