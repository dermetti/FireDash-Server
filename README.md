# FireDash Server

FireDash Server is the secure reference-data service for fire departments. It
provisions departmental and station reference data to authorised tablets; it
does **not** accept, store, or process incident or intervention data.

The service is a Django 5.2 and Django REST Framework application backed by
PostgreSQL/PostGIS. Gunicorn runs behind Nginx, and hardened systemd services
run the publication and PDF-sanitisation boundaries on Debian LXC hosts.

Start here:

- [Documentation index](docs/README.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)
- [Deployment](docs/deployment.md)

The generated API schema is available from a running server at
`/api/v1/schema/`, with interactive documentation at `/api/v1/docs/`.
