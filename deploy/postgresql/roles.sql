-- Run as database_owner after every schema migration operation.
GRANT USAGE ON SCHEMA public TO application_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO application_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO application_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE database_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO application_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE database_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO application_runtime;

GRANT USAGE ON SCHEMA public TO backup_role;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO backup_role;
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
