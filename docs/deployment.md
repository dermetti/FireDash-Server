# Debian LXC Deployment

1. Create the `fire_backend` service account with `deploy/scripts/create-service-users.sh` as root.
2. Install the application at `/opt/fire-backend` and create its virtual environment at `/opt/fire-backend/venv`.
3. Install `requirements/base.txt`; do not run Django or Gunicorn as root.
4. Install the systemd unit, socket, and tmpfiles configuration. Run `systemd-tmpfiles --create`, then enable `fire-backend.socket` and `fire-backend.service`.
5. Create the root-owned environment file described in `configuration.md`.
6. Copy the Nginx site configuration, replace `fire-backend.internal` and certificate paths, test it with `nginx -t`, then reload Nginx.
7. Apply migrations as `database_owner`, run `collectstatic`, apply runtime grants, and verify `https://<host>/health/live` and `/health/ready`.

Gunicorn only listens on its Unix socket. Nginx is the TLS endpoint. PostgreSQL listens locally only. Do not use Django's development server in production.
