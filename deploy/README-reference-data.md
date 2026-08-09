# Reference-data deployment

Install the pinned system package in `apt-packages.txt`, then install the root-owned
`fire-pdf-sanitize` wrapper at `/usr/local/lib/fire-backend/fire-pdf-sanitize` with mode
`0755`. The wrapper must remain root-owned and must not be writable by `fire_backend` or
`fire_pdf_sanitizer`.

The application calls only this wrapper, with a cleared environment. The wrapper creates a
transient systemd service running as `fire_pdf_sanitizer`; it has private networking, a
read-only bind mount of the quarantined input, and a write bind mount only for the generated
job output. Configure systemd/polkit so `fire_backend` may start only this wrapper's transient
unit profile. If that policy or any requested sandbox property is unavailable in the LXC, PDF
uploads must fail; do not replace the wrapper with a direct `qpdf` call.

The Django deployment also requires GEOS and GDAL shared libraries compatible with the installed
Django version because the reference-data models use GeoDjango/PostGIS `PointField`s. Configure
`GDAL_LIBRARY_PATH` when the library is outside Django's normal discovery path. CI and Debian LXC
must run migration and spatial-model tests against PostgreSQL with the PostGIS extension enabled.

`MAX_PDF_INPUT_BYTES` must equal Nginx `client_max_body_size` after conversion to MiB. The
default is 100 MiB. When changing the memory or timeout settings, update the corresponding
`systemd-run` properties in the installed wrapper and validate the Linux sandbox integration
suite.

Accepted fire plans reside in `/var/lib/fire-backend/fire-plans`. Do not add an Nginx alias,
`MEDIA_URL`, or static-file mapping for this directory. Protected download is a later phase.
