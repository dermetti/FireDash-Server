# Production Python dependency lock

`requirements/base.txt` remains the direct input. `production.lock` contains
the complete CPython 3.13, Debian 13 amd64 dependency closure and one reviewed
binary-wheel SHA-256 per package. It is intentionally not a Windows install
file. Windows development continues to use `requirements/dev.txt`; use
`-c requirements/production-constraints.txt` when reproducing production versions.
Those generated constraints contain versions without Linux hashes.

The initial lock preserves all 34 versions in CI artifact 15029872195 (run
45628537680), adds `drf-spectacular-sidecar==2026.10.1`, and explicitly includes
`packaging==26.4`, required by Gunicorn's actual wheel metadata but absent from
that audit artifact. No existing application version was upgraded. The result
contains 36 packages. The artifact is advisory evidence, not a complete install
manifest; wheel metadata and a real resolver determine the install closure.

## Generate or update

Use a disposable development environment with Python 3.13, pip and `packaging`
(provided by the existing development tooling). Native Debian 13 amd64 is the
preferred execution environment. From the repository root:

```sh
python tools/production_lock.py generate
python tools/production_lock.py check
```

Generation invokes the real pip resolver against HTTPS PyPI with explicit
CPython 3.13, cp313/compatible abi3, manylinux_2_17/2_28 x86_64 and universal
wheel support, binary-only and ignore-installed. Existing generated constraints
hold reviewed versions constant. For an intentional update, copy the constraints
to a disposable file, change only reviewed version constraints there and the
applicable direct input, and pass `generate --constraints /path/to/candidate.txt`.
Review both generated files and all version/hash changes together. Never edit
hashes by hand or accept a missing wheel by allowing source builds.

Pip cross-platform options do not consistently change host marker evaluation.
The generator therefore evaluates wheel metadata dependencies and extras against
an explicit Linux/Python 3.13 environment, selects the reachable closure and
rejects missing or conflicting dependencies. Windows-only tzdata/colorama from
the cross-target resolver report are excluded. This is validated cross-target
resolution, not a claim of native Linux execution. Wheel filenames are recorded
in the lock. Each selected hash is checked against PyPI release metadata and
the downloaded distribution bytes; yanked files are rejected.

Before approval, install into a fresh native target environment:

```sh
python3 -m venv /tmp/firedash-production
/tmp/firedash-production/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements/production.lock
/tmp/firedash-production/bin/python -m pip check
```

The mandatory deployment-security job performs this check and imports the
production libraries in a disposable Debian 13 amd64 container. Mandatory CI
application and production-schema jobs first install the same hash-verified lock,
then install development tools constrained to its exact versions and run
`pip check`. Their native PostgreSQL, PDF and Nginx security tests remain intact.
The advisory job audits the exact enumerated production lock, development
resolution and separate ACME lock. Review actual artifacts, including INCOMPLETE
results, before release approval.

The installer creates the virtual environment at its final SHA-addressed path,
uses hash verification/binary-only installation and checks dependencies before
marking the release complete. Failed installs/checks remain incomplete.
Previously completed releases are reused without rebuilding or changing their
packages; a new lock requires a new reviewed release SHA. No installed ACME
environment is automatically replaced. Bootstrap pip itself remains an operator/
Debian-provided installation tool, outside the application runtime lock.

## Installed-host acceptance

Record the actual beta release's `pip list --format=json`, `pip check` and
installed-environment advisory review separately from repository lock results.
Inventory Debian packages with versions and security status, accounting for
Debian security backports rather than comparing upstream version strings alone.
Include system Python, the isolated PDF helper's Debian pikepdf/qpdf/libmagic and
their native dependencies, OpenSSL, Nginx, PostgreSQL 17, PostGIS, GDAL, GEOS,
PROJ and libpq. Review native libraries bundled in cryptography, Pillow, pikepdf,
psycopg-binary and lxml wheels (their notices/build provenance), as Python package
advisory counts do not fully cover those embedded libraries.

Inventory the installed Certbot environment for the selected TLS mode: FireDash's
separate ACME lock when used, or the adopted operator-managed environment and its
approved external DNS plugins. A clean application scan does not clear either.
The existing development VM, previously installed local environment and future
production host require their own evidence. No package-wide Debian pins or new
host update mechanism are introduced.
