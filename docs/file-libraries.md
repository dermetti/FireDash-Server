# File libraries (Stage 1)

`apps.file_library` owns department and station source libraries. The existing
persisted authorization scopes apply; department roles do not inherit station
library access, including when a department administrator has a station assignment.
System administrators have no library access. Suspended resources fail closed.

Libraries are created lazily on first authorized access. A library has exactly one
department or station owner. `Entry` gives folders and logical documents a shared,
case-insensitive sibling namespace, including at root. UUIDs survive renames,
moves and PDF replacements. PostgreSQL constraints and triggers enforce scope,
immutable identities/content metadata, parent kind, cycles and five folder levels.

Every source mutation goes through the services and locks its library row. The
transaction covers canonical entries, current usage, one revision increment and
the audit event. Bulk uploads are independent per-file transactions with individual
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

## Retention and later integration

Replacements create new immutable `Content` objects. Deletion unlinks logical
documents immediately. Old content remains until explicit maintenance:

```
python manage.py collect_library_content --older-than-days 7
```

Collection locks content and uses Django's protected-reference deletion plus
database foreign keys; physical removal happens only after commit. Future retained
publications must reference `Content` with `on_delete=PROTECT`. Current document
references use this same rule. Do not delete files independently of that lifecycle.
Content may be shared by multiple documents within a library; quota counts logical
current content, not unique physical bytes. Stage 1 does not deduplicate uploads.

Filesystem promotion precedes database commit, so ordinary failures clean up newly
promoted files. A process crash or a caller's enclosing transaction rollback can
leave an unreferenced file without a content row. Such files are never served or
counted toward quota; the collector deliberately does not guess whether unknown
filesystem objects are safe to delete. Likewise a failed post-commit unlink can
leave an orphan. Disk monitoring and offline reconciliation remain operational
responsibilities. Retained content is not constrained by the current-library quota.

No publication registry, build, manifest, encryption or tablet integration is
introduced. The source revision and immutable content identities are the future
integration points. Tablet display suffixes are not added to stored library names.
