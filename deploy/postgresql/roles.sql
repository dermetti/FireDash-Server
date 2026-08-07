-- Run as database_owner after schema migrations, and again after adding tables.
GRANT USAGE ON SCHEMA public TO application_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO application_runtime;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO application_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE database_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO application_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE database_owner IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO application_runtime;

-- Phase 2 replaces the broad audit-event privileges with its append-only grants and trigger.
