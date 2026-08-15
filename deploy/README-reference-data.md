# Reference-data deployment

Install the pinned system package in `apt-packages.txt`, then install the root-owned
`fire-pdf-sanitizer-broker` executable at `/usr/local/lib/fire-backend/fire-pdf-sanitizer-broker`
with mode `0755`. The broker must remain root-owned and must not be writable by `fire_backend`
or `fire_pdf_sanitizer`.

The application never elevates privileges. `fire-backend.service` keeps `NoNewPrivileges=true`
and connects to the broker's Unix socket (`/run/fire-pdf-sanitizer-broker/broker.sock`, owned
`root:fire_backend` mode `0660`). The socket uses `Accept=yes` and spawns the
`fire-pdf-sanitizer-broker@.service` per-connection template with the accepted connection on
stdin/stdout. The root broker validates a single lowercase canonical UUID, starts
`fire-pdf-sanitizer@<uuid>.service`, and finalizes the output ownership.
The sanitizer runs as `fire_pdf_sanitizer` with private networking, a read-only bind mount of
the quarantined input, and a write bind mount only for the generated job output. If any
requested sandbox property is unavailable in the LXC, PDF uploads must fail; do not replace the
broker with a direct `qpdf` call.

The Django deployment also requires GEOS and GDAL shared libraries compatible with the installed
Django version because the reference-data models use GeoDjango/PostGIS `PointField`s. Configure
`GDAL_LIBRARY_PATH` when the library is outside Django's normal discovery path. CI and Debian LXC
must run migration and spatial-model tests against PostgreSQL with the PostGIS extension enabled.

`MAX_PDF_INPUT_BYTES` must equal Nginx `client_max_body_size` after conversion to MiB. The
default is 100 MiB. When changing the memory, timeout, or output-size settings, update the
matching `fire-pdf-sanitizer@.service` sandbox properties and the broker's output-size cap, and
validate the Linux sandbox integration suite.

Accepted fire plans reside in `/var/lib/fire-backend/fire-plans`. Do not add an Nginx alias,
`MEDIA_URL`, or static-file mapping for this directory. Protected download is a later phase.
