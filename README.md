# FireDash Server

FireDash Server is the secure reference-data service for fire departments. It
distributes authorized departmental and station reference datasets to tablets.
It also accepts encrypted, structured report submissions from authenticated
tablets. Report Transfer temporarily stores encrypted JSON/JPEG packages and
provides controlled recipient access through a Magic-Link report portal,
separately from publication and reference-data ingestion.

The service is a Django 5.2 and Django REST Framework application backed by
PostgreSQL/PostGIS. Gunicorn runs behind Nginx, and hardened systemd services
run the publication and PDF-sanitisation boundaries on Debian LXC hosts.

Start here:

- [Documentation index](docs/README.md)
- [Architecture](docs/architecture.md)
- [Report Transfer](docs/report-transfer.md)
- [Development](docs/development.md)
- [Deployment](docs/deployment.md)

The generated API schema is available from a running server at
`/api/v1/schema/`, with interactive documentation at `/api/v1/docs/`.
