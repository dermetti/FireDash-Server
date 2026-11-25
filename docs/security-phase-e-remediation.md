# Phase E targeted remediation

Baseline: `579f60ecc61542faeebaa6fe374c1fc09e9dc217` (also the local HEAD).
All fixtures used here are disposable synthetic documents and test credentials.
No migrations, Git mutations, deployments, live-server access, dependency advisory
scans, or full application-suite runs were performed.

## FD-SEC-018: isolated native parsing

File Library uploads and both plan ingestion workflows now call the shared
`storage.process_pdf` boundary. Django checks the extension, byte limit and five-byte
signature, writes quarantine data, and delegates the job. It never calls libmagic
or pikepdf on uploaded or sanitized PDFs. After success, it checks bounded metadata,
the output signature, size, page limit and SHA-256 without a PDF parser.

The existing root broker still accepts only one canonical UUID. It starts the same
fixed unprivileged systemd service; it does not parse PDFs. The service now invokes
`/usr/bin/python3 -I /usr/local/lib/fire-backend/pdf/pdf_worker.py`. Bootstrap installs
only that worker and the shared `pdf_validation.py` policy, owned by root, beneath a
root-owned directory. The application release, virtualenv, environment and credentials
are not made available to the service. The worker refuses execution as root.

The worker applies the existing MIME, structural, encryption, page-count and prohibited
content policy to input, invokes qpdf with the existing normalization flags, then
applies the same validation to output. Strict File Library inspection and ingestion's
supported structural recovery use the same policy with the existing `strict` switch.
Memory (512 MiB), CPU quota (100%), task (32) and unit execution (60 seconds) limits
remain. The qpdf child also has a 45-second timeout. The report spool is explicitly
inaccessible, alongside accepted documents, publications, configuration and releases.

The UUID socket request is unchanged. A job-local request file contains only strict
mode and existing numeric PDF limits, capped at 256 bytes and validated by the worker.
It cannot specify paths, commands or units. Successful processing writes a single
ASCII result line of at most 128 bytes:

```text
OK <size> <pages> <lowercase-sha256>
ERR content <allowlisted-reason-code>
```

The broker opens the result through pinned directory descriptors, rejects symlinks,
special files, multiple links, oversized and malformed results, and preserves
descriptor-based output ownership/mode finalization (backend owner, mode 0600).
Only a successful service execution with an explicit validated content result can
produce a skippable rejection. Nonzero systemd/qpdf results, timeouts, OOM/operational
exceptions, missing dependencies/results and malformed replies remain infrastructure
failures. In particular, qpdf exit 2 is never interpreted as a content verdict.
The web boundary converts positive rejections to the existing content-validation
error contract, retaining reason codes. Package partial acceptance remains supported;
infrastructure failures abort preview. Job cleanup removes request/result artifacts too.

Debian's actual package metadata was checked: [python3-pikepdf](https://packages.debian.org/trixie/python3-pikepdf)
is 9.5.2 on trixie/amd64, depends on system Python >=3.13 and <3.14 and libqpdf30;
[python3-magic](https://packages.debian.org/trixie/python3-magic) provides the
`magic.from_file` interface. [Debian 13's default Python](https://packages.debian.org/trixie/python3)
is 3.13. Bootstrap installs these packaged dependencies and verifies imports with
the actual isolated system interpreter. This is package/interpreter verification,
not evidence of native execution on this Windows host.

## FD-SEC-019: PDF name-value traversal

A disposable probe reproduced acceptance of `/S /JavaScript`, `/S /Launch`, `/S /GoToR`
and `/S /URI` when a matching prohibited dictionary key was absent.
The shared traversal now recognizes typed pikepdf names, dictionaries, arrays and
stream dictionaries. It applies the existing prohibited-name set to name values and
dictionary keys, retains indirect-object cycle handling, and does not treat PDF
strings as names. Synthetic tests cover nested dictionary/array/stream actions,
strict and recovery modes, input and output inspection, ordinary text, internal
GoTo/Link navigation and indirect cycles. No rewriting or annotation removal was added.

## FD-SEC-020: immutable accepted content and replacement

All three workflows use the shared quarantine/processing boundary and the same
exclusive accepted-storage promotion helper. Promotion cannot overwrite an existing
physical identity, including a generated-key collision. KLGV replacement now creates
a new `plans/<fresh-uuid>.pdf` key while retaining the canonical document UUID. Fire
Plan and File Library canonical identities, folder structure and fresh content
identities remain unchanged.

Ingestion copies and rechecks the hash-bound staged PDF before promotion, preserving
the preview for retry after rollback. New accepted identities are tracked around the
database transaction. Failed application removes only unreferenced new content;
previously accepted bytes are never overwritten or removed on rollback. Cleanup
checks persisted references, including retained publication sources, and retains bytes
when database state is uncertain. A failure in a post-commit callback cannot delete
already committed content. File Library failure cleanup uses the same reference guard.

Plan replacement and explicit plan deletion share post-commit retirement. Canonical
references and retained publication snapshots prevent removal of needed source bytes;
publication ciphertext and artifact references are untouched. File Library continues
its existing Content/PublicationContent garbage-collection and orphan-reconciliation
contracts. Retained plan sources are conservatively kept; this change does not add a
bulk historical-content sweep or rename/reprocess existing accepted PDFs.

Intentional differences:

- File Library requires structurally strict, nonempty PDFs and applies its existing
  per-file and library quota rules; plan ingestion retains supported recovery of
  repairable PDFs and its existing package limits/partial acceptance and review flow.
- Plan previews stage accepted output for later authorized review/application. Library
  uploads apply directly within their existing locked transaction.
- Fire Plan and KLGV operational metadata and merge rules remain domain-specific.
  File Library preserves folders and Content references. The common storage boundary
  does not merge domain models or change publication/tablet contracts.

Existing storage keys remain valid. Hard-link-based exclusive promotion requires
quarantine/output and accepted storage on the same filesystem with hard-link support;
the previous `os.replace` promotion already required a shared filesystem. The broker's
existing 150 MiB output cap remains. Worker policy numbers must be nonnegative 31-bit
integers. Root-owned helper, service, broker and application updates must be installed
together: an old/missing worker result fails closed. The application virtualenv pins
pikepdf 10.3.0 while Debian's worker uses 9.5.2; real Debian smoke acceptance is required
for that runtime combination.

## Verification

Final focused pytest run: **283 passed, 7 skipped, 0 failed** in 124.16 seconds.
The skips are one native qpdf test (executable unavailable) and six Linux broker
descriptor/ownership/FIFO tests (Windows host). This used
`DJANGO_SETTINGS_MODULE=config.settings.test`, the configured local PostgreSQL test
database and `-p no:cacheprovider` with the repository's fixed scratch base.
Focused coverage includes:

- PDF policy, worker, client, broker and private storage.
- Shared upload/replacement across all three document kinds, real synthetic unsafe PDFs,
  no web-process native inspection, mandatory broker processing, output/staging integrity,
  stable canonical/fresh physical identity, rollback, post-commit failure and retained artifacts.
- Existing File Library authorization, quotas, publication retention and replacement tests.
- Package parsing, partial acceptance, review wizard, ingestion services and safe diagnostics.
- Plan lifecycle and deployment-helper contracts, plus Windows smoke-command refusal.

Ruff and formatting cover every changed Python file and the broker script. Mypy uses
`--platform linux` on the same 20 files. Bandit covers the nine changed runtime Python
files/scripts; its fixed-command subprocess exclusions are narrow, documented call sites.
Bandit emits its existing explanatory-comment warning for the storage mode suppression,
but reports no security findings. Shell syntax checks and the isolated
`deploy/tests/test_verify_django_checks.sh` fixture pass using Git Bash. These shell
fixtures execute no host deployment or native sanitizer service.
Changed-file Ruff/format checks, Linux-platform mypy (20 files), Bandit (nine runtime
files/scripts), and `git diff --check` all passed. There are no remaining local test
failures; native acceptance and subsequent full GitHub CI remain pending.

The baseline [GitHub Actions run 45595708184](https://github.com/dermetti/FireDash-Server/actions/runs/45595708184)
matches the full baseline SHA and completed successfully: server-tests, deployment-security,
static-checks, dependency-audit and acme-environment all passed. No attributable CI
regression was found. The previously corrected native Nginx test and production Nginx/TLS
configuration are unchanged. Full regression testing of these uncommitted changes remains
with subsequent GitHub CI.

## Remaining native acceptance

These checks remain pending on a disposable Debian 13 fixture, not an existing server:

1. Install the updated packaged dependencies, root-owned helper/broker and unit through
   the normal bootstrap flow. Check helper directory `root:root:0755`, files
   `root:root:0644`, broker ownership, socket permissions and effective unit hardening.
   Confirm `/usr/bin/python3 -I` imports the distro pikepdf/magic packages without access
   to application configuration, environment, virtualenv or release files.
2. Run `verify_library_pdf_sanitizer` as `fire_backend` with the disposable application
   configuration. `deploy/verify-deployment.sh` now requires this exact smoke operation
   and propagates failure. It exercises the real broker/service/qpdf and both strict
   and recovery modes: benign, non-PDF, malformed, encrypted, name-only prohibited action,
   and repairable cross-reference fixtures. Every job must clean its artifacts; no
   canonical/publication records are created.
3. Execute the native qpdf and Linux broker descriptor tests in normal test discovery.
   Verify symlink/hard-link/FIFO/directory-substitution rejection, output ownership/modes,
   and pinned-descriptor finalization with the real kernel.
4. Exercise end-to-end PDF package partial acceptance through the real service, then
   stop/fail the worker or remove its result: positive content rejection may skip a
   member; ambiguous failures must abort preview with no promotion. Check actual timeout,
   cgroup resource enforcement and absence of native PDF parsing in the web/root broker.
5. Verify the sanitizer cannot read accepted/library content, publications, report spool,
   configuration or the application release, and cannot escape its writable job mount or
   obtain application credentials/network access. Mocked unit assertions are not this
   native sandbox acceptance.

## Exact changed-file inventory

Runtime and domain integration:

- `apps/reference_data/pdf_validation.py`
- `apps/reference_data/pdf_worker.py` (new)
- `apps/reference_data/pdf_sandbox.py`
- `apps/reference_data/storage.py`
- `apps/reference_data/services.py`
- `apps/file_library/services.py`
- `apps/file_library/management/commands/verify_library_pdf_sanitizer.py`
- `apps/ingestion/services.py`

Regression coverage:

- `apps/reference_data/tests/test_pdf_validation.py`
- `apps/reference_data/tests/test_pdf_worker.py` (new)
- `apps/reference_data/tests/test_pdf_sandbox.py`
- `apps/reference_data/tests/test_pdf_sanitizer_broker.py`
- `apps/reference_data/tests/test_storage.py`
- `apps/file_library/tests/test_libraries.py`
- `apps/ingestion/tests/test_partial_documents.py`
- `apps/ingestion/tests/test_review_wizard.py`
- `apps/ingestion/tests/test_services.py`
- `apps/ingestion/tests/test_pdf_lifecycle.py` (new)
- `deploy/tests/test_pdf_sanitizer.py` (new)
- `deploy/tests/test_verify_django_checks.sh`

Deployment boundary and report:

- `deploy/apt-packages.txt`
- `deploy/bootstrap-lxc.sh`
- `deploy/scripts/fire-pdf-sanitizer-broker`
- `deploy/systemd/fire-pdf-sanitizer@.service`
- `deploy/verify-deployment.sh`
- `docs/security-phase-e-remediation.md` (new)
