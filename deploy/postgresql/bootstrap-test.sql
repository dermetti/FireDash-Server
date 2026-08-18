-- CI/development only. Run as a PostgreSQL superuser after production bootstrap.
-- Never run this file on a production host.
\if :{?application_runtime_password}
\else
\quit 3
\endif
\if :{?firedash_test_password}
\else
\quit 3
\endif

SELECT format(
    'CREATE ROLE application_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'application_runtime_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'application_runtime')
\gexec

SELECT format(
    'CREATE ROLE firedash_test LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'firedash_test_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'firedash_test')
\gexec

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'firedash_test'
          AND (NOT rolcanlogin OR rolsuper OR NOT rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'firedash_test has unsafe attributes';
    END IF;
END $$;

SELECT 'CREATE DATABASE firedash_test_template OWNER database_owner TEMPLATE template0 ENCODING ''UTF8'' LC_COLLATE ''C.utf8'' LC_CTYPE ''C.utf8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'firedash_test_template')
\gexec

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_database
        WHERE datname = 'firedash_test_template'
          AND (pg_encoding_to_char(encoding) <> 'UTF8' OR datcollate <> 'C.utf8' OR datctype <> 'C.utf8')
    ) THEN
        RAISE EXCEPTION 'firedash_test_template has incompatible encoding/locale; recreate the disposable test template as UTF-8 before running tests';
    END IF;
END $$;

\connect fire_backend
ALTER DATABASE firedash_test_template ALLOW_CONNECTIONS true;
\connect firedash_test_template
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS btree_gist;

\connect fire_backend
ALTER DATABASE firedash_test_template IS_TEMPLATE true;
ALTER DATABASE firedash_test_template ALLOW_CONNECTIONS false;
