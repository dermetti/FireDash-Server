-- Run as database_owner after schema migrations, and again after adding tables.
GRANT USAGE ON SCHEMA public TO application_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO application_runtime;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO application_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE database_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO application_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE database_owner IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO application_runtime;

REVOKE ALL ON TABLE audit_event FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_event FROM application_runtime;
GRANT SELECT, INSERT ON TABLE audit_event TO application_runtime;
