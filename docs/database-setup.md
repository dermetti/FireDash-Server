# PostgreSQL/PostGIS Setup

On Debian 13, install PostgreSQL 17 and PostGIS 3.5. Bind PostgreSQL to localhost only and use SCRAM password authentication. The installer runs `deploy/postgresql/bootstrap-production.sql` as a PostgreSQL superuser, supplying the three role passwords through the environment (`FIREDASH_DATABASE_OWNER_PASSWORD`, `FIREDASH_APPLICATION_RUNTIME_PASSWORD`, `FIREDASH_BACKUP_ROLE_PASSWORD`), which the SQL reads via `\getenv`. It creates only the production roles and the `fire_backend` database, with `postgis` and `btree_gist` enabled.

Run Django migrations using `database_owner`; run Gunicorn and application workers using `application_runtime`. Apply `deploy/postgresql/roles.sql` as `database_owner` after every migration operation so runtime and backup privileges cover new objects while audit protections remain in force.

`deploy/postgresql/bootstrap-test.sql` is CI/development only. It creates `firedash_test`, the only role with `CREATEDB`, and a non-connectable `firedash_test_template` database with `postgis` and `btree_gist` enabled. Test settings clone disposable databases from that template. Never run this file or use the test role, template, or its password on a production host.

The runtime role must not be a superuser, schema owner, `CREATEDB`, `CREATEROLE`, replication, or `BYPASSRLS` role. It has normal application CRUD access, but audit rows remain append-only: runtime may `SELECT` and `INSERT`, while PostgreSQL grants and the `audit_event_immutable` trigger reject `UPDATE`, `DELETE`, and `TRUNCATE`. The immutable dataset-type registry is runtime read-only. `backup_role` has read-only schema/table/sequence privileges sufficient for the custom-format `pg_dump` workflow.
