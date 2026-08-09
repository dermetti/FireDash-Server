#!/bin/sh
set -eu

if ! getent group fire_backend >/dev/null; then
    groupadd --system fire_backend
fi

if ! id fire_backend >/dev/null 2>&1; then
    useradd --system --gid fire_backend --home-dir /var/lib/fire-backend \
        --shell /usr/sbin/nologin --no-create-home fire_backend
fi

if ! getent group fire_pdf_sanitizer >/dev/null; then
    groupadd --system fire_pdf_sanitizer
fi

if ! id fire_pdf_sanitizer >/dev/null 2>&1; then
    useradd --system --gid fire_pdf_sanitizer --home-dir /nonexistent \
        --shell /usr/sbin/nologin --no-create-home fire_pdf_sanitizer
fi

install -d -o fire_backend -g fire_backend -m 0750 /var/lib/fire-backend/static
install -d -o root -g fire_backend -m 0750 /etc/fire-backend
install -d -o fire_backend -g fire_pdf_sanitizer -m 0750 /var/lib/fire-backend/quarantine
install -d -o fire_backend -g fire_pdf_sanitizer -m 0750 /var/lib/fire-backend/sanitizer-output
install -d -o fire_backend -g fire_backend -m 0750 /var/lib/fire-backend/fire-plans
echo "Created service account and runtime directories."
