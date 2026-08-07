# PostgreSQL/PostGIS Setup

Install PostgreSQL and the matching PostGIS package on Debian. Bind PostgreSQL to localhost only. Run `deploy/postgresql/bootstrap.sql` once as a PostgreSQL superuser after replacing placeholder passwords through a secure process. Phase 1 verifies the PostGIS extension through readiness; GeoDjango and its GDAL dependency are introduced with the first spatial model in Phase 5.

Run Django migrations using `database_owner`; run Gunicorn using `application_runtime`. Apply `deploy/postgresql/roles.sql` as `database_owner` after migrations so the runtime role receives only required data privileges.

The runtime role must not be a superuser, schema owner, or `BYPASSRLS` role. Phase 2 will add the more restrictive, append-only privileges and trigger for audit events.
