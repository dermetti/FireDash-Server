# File libraries (Stages 1–3)

`apps.file_library` owns department and station source libraries. The existing
persisted authorization scopes apply; department roles do not inherit station
library access, including when a department administrator has a station assignment.
System administrators have no library access. Suspended resources fail closed.

Department administrators may activate one audited temporary station-library
grant from their department library after explicit confirmation and recent
reauthentication. The persisted grant is bound to the user and station and has
an absolute six-hour expiry. It permits browsing, search, preview, download,
and one successful recursive deletion only. Switching stations, explicit
release, expiry, loss of department authority, another department-library
mutation, or that successful deletion revokes the grant. This capability is
separate from normal scope authorization, publication authorization, and tablet
installation entitlements.

Libraries are created lazily on first authorized access. A library has exactly one
department or station owner. `Entry` gives folders and logical documents a shared,
case-insensitive sibling namespace, including at root. UUIDs survive renames,
moves and PDF replacements. PostgreSQL constraints and triggers enforce scope,
immutable identities/content metadata, parent kind, cycles and five folder levels.

Every source mutation goes through the services and locks its publication scope
before its library row. The transaction covers canonical entries, current usage,
one revision increment, the audit event and the coalesced publication intent.
The scope revision is synchronized to the library revision. Bulk uploads are independent per-file transactions with individual
feedback. Recursive deletion is one mutation and its audit records every removed
identity. No-op edits do not advance revisions. Current usage counts each logical
document's sanitized content size; historical/unreferenced content is excluded.

## Storage and configuration

- `FILE_LIBRARY_MAX_FILE_BYTES`: default 25 MiB, enforced on both input and output.
  The existing `MAX_PDF_INPUT_BYTES` / `MAX_PDF_OUTPUT_BYTES` remain upper bounds.
- `FILE_LIBRARY_QUOTA_BYTES`: default 1 GiB, copied into newly created libraries.
  Existing quotas remain persisted; there is no administrator quota interface.
- Existing quarantine, broker, sanitizer-output and accepted-storage settings apply.
  Accepted keys are `file-libraries/<library UUID>/<content UUID>.pdf`, beneath the
  private `REFERENCE_DATA_ACCEPTED_ROOT`, never beneath static or public media.

Input and sanitized output undergo MIME, signature, strict PDF structure,
encryption and active-content validation. The existing Linux broker is mandatory;
there is no unsandboxed production fallback. Downloads/previews require live scope
authorization and verify size and SHA-256 before serving with private/no-store
headers. Client filenames never become storage paths.

## Publication and retention

`department_files` and `station_files` are required, schema-2
`document-manifest-v2` datasets. The existing build worker discovers active
departments/stations lacking scopes, including libraries never opened in the
portal. Initial empty libraries publish normally. Existing Stage 1 revisions
are preserved when their publication scopes are initialized. Source edits queue
the normal next nightly build window; Publish Now expedites the same intent.
Neither UI state nor a published generation confirms tablet receipt.

The worker freezes the complete source tree under the publication scope lock,
then creates `PublicationContent` protected references before committing its
BUILDING transition. Source edits take that lock first, so a frozen tree cannot
combine revisions. In-progress builds and retained successful generations pin
immutable sanitized content independently of current entries. A source revision
change during encryption retires the candidate without activating it and queues
a fresh intent. Failures preserve the current successful generation.

The generic document adapter uses frozen logical UUIDs, metadata and pinned
content, never a current-document lookup. Unchanged document/hash pairs reuse
the existing per-document encrypted artifact. Library/folder/name changes still
produce new signed authoritative manifests. PostgreSQL guards validate scope,
frozen references, ready-generation completeness and immutable source/crypto
metadata. No monolithic archive is created. The wire contract is specified in
`docs/tablet-api.md`.

Normal publication retention keeps the current generation and the two most recent
successful predecessors (`PUBLICATION_RETAINED_ROLLBACK_PREDECESSORS=2`). Terminal
unsuccessful attempts do not count. Retiring a generation releases its content
pins and standard encrypted-artifact references; the existing artifact cleanup
infrastructure handles final-reference removal. Historical file-library downloads
and grants remain usable while retained, but every request rechecks the current
installation/department/station entitlement. Station reassignment immediately
denies old station-library identifiers. Administrative scope checks also apply
to direct generic publication lifecycle URLs and services.

Replacements create new immutable `Content` objects. Deletion unlinks logical
documents immediately. Old content remains until explicit maintenance:

```
python manage.py collect_library_content --older-than-days 7
```

Collection locks content and uses Django's protected-reference deletion plus
database foreign keys; physical removal happens only after commit. Publication
content pins reference `Content` with `on_delete=PROTECT`. Current document
references use this same rule. Do not delete files independently of that lifecycle.
Content may be shared by multiple documents within a library; quota counts logical
current content, not unique physical bytes. Stage 1 does not deduplicate uploads.

## Accepted-storage reconciliation

Filesystem promotion still precedes database commit. Ordinary failures remove
newly promoted files; crashes, an enclosing transaction rollback, or failed
post-commit unlink can leave an orphan. Run the narrow reconciliation command
as the accepted-storage owner, alongside the existing maintenance jobs:

```
python manage.py reconcile_library_storage --older-than-days 7 --dry-run
python manage.py reconcile_library_storage --older-than-days 7
```

It scans only `file-libraries/<UUID>/<UUID>.pdf`, skips symlinks and noncanonical
paths, and keeps every file with a durable `Content` row, whether current,
unreferenced, building or retained. It requires at least one day's grace (seven
days by default), rechecks references while holding the same per-library
PostgreSQL transaction advisory lock as promotion, and is idempotent. This lock
also protects an uncommitted library/content insertion: cleanup waits for commit
or rollback, even if the library row itself is not yet visible. It safely handles
directories left by rolled-back library creation. Quarantine and sanitizer output
remain governed by their existing cleanup lifecycle. Unknown layouts are ignored.

Deploy the promotion lock and reconciliation code together; drain old upload
workers before the first cleanup pass. Schedule the command with accepted-root
write permissions, not by broadening the publication worker's read-only accepted
storage access. Retained plaintext and encrypted artifacts remain outside the
current-source quota, so physical disk capacity still needs ordinary monitoring.

## Deployment validation

Windows tests stub native MIME identification and the Linux sanitizer broker;
they exercise real pikepdf inspection and the real PostgreSQL/crypto lifecycle.
Before deployment, run a real Linux upload-to-publication check with libmagic,
the isolated broker, configured UID/GID boundaries and accepted-storage modes:

1. Upload a benign PDF through each role's library endpoint and verify sanitized
   bytes are readable by the publication worker but never publicly served.
2. Verify non-PDF, malformed, encrypted/password-protected and sanitizer-failure
   inputs fail without canonical entries, quota usage or publication mutations.
3. Publish through the worker, obtain a grant through an authorized test installation,
   and verify signed metadata, ciphertext hash and decrypted sanitized PDF hash.
4. Replace/delete source content, exercise retained-history cleanup, and run
   reconciliation with a deliberate aged orphan and an in-progress upload.

No iPad browser or storage telemetry is implemented. The Stage 3 client must
support these required types before activating a complete manifest containing
them, and render the department library with ` (global)` appended for display.
The suffix is never stored in the configurable name or signed source metadata.
