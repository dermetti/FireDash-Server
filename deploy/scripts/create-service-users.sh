#!/bin/sh
set -eu

if ! getent group fire_backend >/dev/null; then
    groupadd --system fire_backend
fi

if ! id fire_backend >/dev/null 2>&1; then
    useradd --system --gid fire_backend --home-dir /var/lib/fire-backend \
        --shell /usr/sbin/nologin --no-create-home fire_backend
fi

install -d -o fire_backend -g fire_backend -m 0750 /var/lib/fire-backend/static
install -d -o root -g fire_backend -m 0750 /etc/fire-backend
echo "Created service account and runtime directories."
