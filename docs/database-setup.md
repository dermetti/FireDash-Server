# PostgreSQL/PostGIS Setup

Install PostgreSQL and the matching PostGIS package on Debian. Bind PostgreSQL to localhost only. Run `deploy/postgresql/bootstrap.sql` once as a PostgreSQL superuser after replacing placeholder passwords through a secure process. Phase 1 verifies the PostGIS extension through readiness; GeoDjango and its GDAL dependency are introduced with the first spatial model in Phase 5.

Run Django migrations using `database_owner`; run Gunicorn using `application_runtime`. Apply `deploy/postgresql/roles.sql` as `database_owner` after migrations so the runtime role receives only required data privileges.

`bootstrap.sql` also creates `firedash_test`, the only role with `CREATEDB`, and a non-connectable
`firedash_test_template` database with PostGIS enabled. Test settings clone disposable databases
from that template. Do not use the test role, template, or its password in deployed application
services.

The runtime role must not be a superuser, schema owner, `CREATEDB`, or `BYPASSRLS` role. Phase 2 will add the more restrictive, append-only privileges and trigger for audit events.
