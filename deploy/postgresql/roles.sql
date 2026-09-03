-- Run as database_owner after every schema migration operation.
GRANT USAGE ON SCHEMA public TO application_runtime;
GRANT USAGE ON SCHEMA public TO backup_role;

-- Do not use GRANT ... ON ALL ... IN SCHEMA public here. PostGIS installs
-- extension-owned relations in public (for example spatial_ref_sys and its
-- compatibility views), which database_owner must neither own nor grant.
-- Reapply privileges only to FireDash objects owned by database_owner.
DO $$
DECLARE
    object_record record;
BEGIN
    FOR object_record IN
        SELECT namespace.nspname, relation.relname
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_roles AS owner ON owner.oid = relation.relowner
        WHERE namespace.nspname = 'public'
          AND owner.rolname = 'database_owner'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
    LOOP
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO application_runtime',
            object_record.nspname,
            object_record.relname
        );
        EXECUTE format(
            'GRANT SELECT ON TABLE %I.%I TO backup_role',
            object_record.nspname,
            object_record.relname
        );
    END LOOP;

    FOR object_record IN
        SELECT namespace.nspname, relation.relname
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_roles AS owner ON owner.oid = relation.relowner
        WHERE namespace.nspname = 'public'
          AND owner.rolname = 'database_owner'
          AND relation.relkind = 'S'
    LOOP
        EXECUTE format(
            'GRANT USAGE, SELECT ON SEQUENCE %I.%I TO application_runtime',
            object_record.nspname,
            object_record.relname
        );
        EXECUTE format(
            'GRANT USAGE, SELECT ON SEQUENCE %I.%I TO backup_role',
            object_record.nspname,
            object_record.relname
        );
    END LOOP;
END $$;

ALTER DEFAULT PRIVILEGES FOR ROLE database_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO application_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE database_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO application_runtime;

ALTER DEFAULT PRIVILEGES FOR ROLE database_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO backup_role;
ALTER DEFAULT PRIVILEGES FOR ROLE database_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO backup_role;

REVOKE ALL ON TABLE audit_event FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_event FROM application_runtime;
GRANT SELECT, INSERT ON TABLE audit_event TO application_runtime;

-- This table is a projection of the immutable code-level dataset registry.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE publications_datasettyperegistry
    FROM application_runtime;
GRANT SELECT ON TABLE publications_datasettyperegistry TO application_runtime;
