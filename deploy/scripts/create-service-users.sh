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

if ! getent group fire_publication >/dev/null; then
    groupadd --system fire_publication
fi

if ! getent group fire_document_readers >/dev/null; then
    groupadd --system fire_document_readers
fi

if ! getent group fire_nginx >/dev/null; then
    groupadd --system fire_nginx
fi

if ! id fire_publication >/dev/null 2>&1; then
    useradd --system --gid fire_publication --home-dir /nonexistent \
        --shell /usr/sbin/nologin --no-create-home fire_publication
fi

install -d -o fire_backend -g fire_nginx -m 0710 /var/lib/fire-backend
install -d -o root -g root -m 0755 /var/lib/fire-backend/static
install -d -o root -g fire_backend -m 0750 /etc/fire-backend
install -d -o fire_backend -g fire_pdf_sanitizer -m 2710 /var/lib/fire-backend/quarantine
install -d -o fire_backend -g fire_pdf_sanitizer -m 2710 /var/lib/fire-backend/sanitizer-output
install -d -o fire_backend -g fire_document_readers -m 2750 /var/lib/fire-backend/fire-plans
install -d -o fire_backend -g fire_document_readers -m 2750 /var/lib/fire-backend/fire-plans/plans
install -d -o fire_backend -g fire_backend -m 0750 /var/lib/fire-backend/import-staging
usermod -a -G fire_document_readers fire_backend
usermod -a -G fire_document_readers fire_publication
# Older releases used the broad backend group for publication source access.
# Remove only that obsolete supplemental membership; no backend configuration
# or secrets become visible through the new reader group.
if id -nG fire_publication | tr ' ' '\n' | grep -qx fire_backend; then
    gpasswd -d fire_publication fire_backend
fi
usermod -a -G fire_nginx fire_publication
usermod -a -G fire_nginx www-data

# Converge only the accepted document source tree. Directories need group
# traverse/read plus setgid inheritance; accepted PDFs need group read only.
# Do not follow links or touch non-PDF content.
find /var/lib/fire-backend/fire-plans -xdev -type d -exec chown fire_backend:fire_document_readers {} + -exec chmod 2750 {} +
find /var/lib/fire-backend/fire-plans -xdev -type f -name '*.pdf' -exec chown fire_backend:fire_document_readers {} + -exec chmod 0640 {} +
install -d -o fire_publication -g fire_nginx -m 2750 /var/lib/fire-backend/publications
install -d -o fire_publication -g fire_publication -m 0700 /var/lib/fire-backend/publications/.tmp
install -d -o root -g root -m 0700 /etc/fire-backend/credentials
echo "Created service accounts and runtime directories."
