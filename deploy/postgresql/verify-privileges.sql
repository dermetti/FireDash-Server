-- Non-mutating effective privilege verification. Run as database_owner.
\set ON_ERROR_STOP on
DO $$
DECLARE
    role_name text;
    relation record;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['application_runtime', 'backup_role'] LOOP
        IF NOT EXISTS (
            SELECT FROM pg_roles WHERE rolname = role_name AND rolcanlogin
            AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
        ) THEN
            RAISE EXCEPTION 'Unsafe or missing service role: %', role_name;
        END IF;
        -- These service identities require no inherited or assumable roles.
        -- Detect unexpected memberships; never remove administrator-managed grants.
        IF EXISTS (
            SELECT FROM pg_roles WHERE rolname <> role_name
            AND (pg_has_role(role_name, oid, 'MEMBER') OR pg_has_role(role_name, oid, 'SET'))
        ) THEN
            RAISE EXCEPTION 'Unexpected effective role membership: %', role_name;
        END IF;
        IF has_schema_privilege(role_name, 'public', 'CREATE')
            OR has_database_privilege(role_name, current_database(), 'CREATE') THEN
            RAISE EXCEPTION 'Service role can create application schema objects: %', role_name;
        END IF;
    END LOOP;

    -- Resolve mandatory relations explicitly so missing tables cannot pass vacuously.
    IF NOT has_table_privilege('application_runtime', 'public.django_migrations', 'SELECT')
        OR NOT has_table_privilege('application_runtime', 'public.publications_datasettyperegistry', 'SELECT')
        OR NOT has_table_privilege('application_runtime', 'public.audit_event', 'SELECT')
        OR NOT has_table_privilege('application_runtime', 'public.audit_event', 'INSERT') THEN
        RAISE EXCEPTION 'Required runtime read/append privileges missing';
    END IF;
    FOREACH role_name IN ARRAY ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'] LOOP
        IF has_table_privilege('application_runtime', 'public.django_migrations', role_name)
            OR has_table_privilege('application_runtime', 'public.publications_datasettyperegistry', role_name) THEN
            RAISE EXCEPTION 'Runtime can mutate protected registry or migration history: %', role_name;
        END IF;
    END LOOP;
    IF has_any_column_privilege('application_runtime', 'public.django_migrations', 'INSERT,UPDATE')
        OR has_any_column_privilege('application_runtime', 'public.publications_datasettyperegistry', 'INSERT,UPDATE')
        OR has_any_column_privilege('application_runtime', 'public.audit_event', 'UPDATE')
        OR has_table_privilege('application_runtime', 'public.audit_event', 'UPDATE,DELETE,TRUNCATE') THEN
        RAISE EXCEPTION 'Runtime has effective protected-table mutation privileges';
    END IF;

    FOR relation IN
        SELECT c.oid, c.relkind, c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relowner = 'database_owner'::regrole
        AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
    LOOP
        IF relation.relkind = 'S' THEN
            IF NOT has_sequence_privilege('backup_role', relation.oid, 'SELECT')
                OR has_sequence_privilege('backup_role', relation.oid, 'USAGE,UPDATE') THEN
                RAISE EXCEPTION 'Unsafe backup sequence privileges: %', relation.relname;
            END IF;
            IF NOT has_sequence_privilege('application_runtime', relation.oid, 'USAGE') THEN
                RAISE EXCEPTION 'Runtime sequence access missing: %', relation.relname;
            END IF;
        ELSE
            IF NOT has_table_privilege('backup_role', relation.oid, 'SELECT')
                OR has_table_privilege('backup_role', relation.oid, 'INSERT,UPDATE,DELETE,TRUNCATE')
                OR has_any_column_privilege('backup_role', relation.oid, 'INSERT,UPDATE') THEN
                RAISE EXCEPTION 'Unsafe backup table privileges: %', relation.relname;
            END IF;
        END IF;
    END LOOP;
    FOREACH role_name IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'] LOOP
        IF NOT has_table_privilege('application_runtime', 'public.organizations_department', role_name) THEN
            RAISE EXCEPTION 'Runtime ordinary application DML missing: %', role_name;
        END IF;
    END LOOP;
END $$;
