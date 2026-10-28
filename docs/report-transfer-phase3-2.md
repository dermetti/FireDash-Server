# Report Transfer Phase 3.2 — engineering report

New submissions require **ReportDocument v1 inside the decrypted JSON report**.
The five-member JWE, protected header (`schema_version: transport-json-v1`),
encryption, recipient policy, HTTP endpoints and idempotency rules from
[Phase 3.1](report-transfer-phase3-1.md) are unchanged. There is no report-type
registry, form catalog or type-specific rendering branch. `report_type` is an
opaque string of 1–80 characters. Adding a type requires only client content.
The v1 validator is `apps/report_transfer/documents.py`.

## Schema and example

[Complete synthetic JSON example](examples/report-document-v1.json) includes all
four block kinds and a signature reference. It defines presentation, not approved
incident/business fields. Clients own ordered arrays; the server neither sorts
their contents nor changes finalized data. All object members shown below are
required unless explicitly optional. Unknown members are rejected.

| Object | Members and bounds |
| --- | --- |
| Document | `report_id` canonical UUID matching transport; integer `schema_version: 1`; `report_type`; `title` (1–200); `header`, `sections`, `attachments`, `finalization` |
| Header / fields | Ordered arrays, 0–100 entries. Each entry: `key`, `label` (1–200), `value` (typed value below) |
| Section | `key`, `title` (1–200), ordered `blocks` (0–50). Maximum 50 sections, 200 blocks overall |
| Text block | `key`, `kind: text`, `text` (0–20,000) |
| Fields block | `key`, `kind: fields`, `fields` (same structure as header) |
| Table block | `key`, `kind: table`, ordered `columns` (1–8), ordered `rows` (0–500). Column: `key`, `label` (1–80). Each row is an ordered array of typed values with exactly one cell per column. Maximum 4,000 table data cells overall |
| Attachment refs block | `key`, `kind: attachment_refs`, `attachment_ids` (0–32 unique UUIDs from document attachments) |
| Attachment display entry | `id` (canonical UUID), `title` (1–200), `caption` (0–2,000). Ordered array; IDs unique; exact set equality with validated transport JPEG IDs. No URLs, paths, arbitrary filenames or embedded markup |
| Finalization | `author: {id, display_name}` (1–128 / 1–160 characters), `finalized_at` (timestamp); optional `signature_attachment_id` referencing an included JPEG |

Keys match `[A-Za-z0-9._-]{1,64}`. Section and block keys share one unique namespace
per document. Header keys, each fields collection and each table's column keys
are unique within their respective collections. Attachment references may be
reused in different blocks; they cannot repeat within one refs block. Every
transport JPEG must have display metadata, even if no block references it.

Each typed value is exactly `{"type": TYPE, "value": VALUE}`:

| Type | JSON value |
| --- | --- |
| `text` | String, 0–4,000 characters |
| `integer` | Integer, absolute value at most 9,007,199,254,740,991; booleans rejected |
| `decimal` | ASCII decimal **string**, at most 32 characters; `-?(0\|[1-9][0-9]*)(\.[0-9]+)?`; no exponent, leading plus or leading zeros |
| `boolean` | JSON boolean |
| `date` | Valid `YYYY-MM-DD` |
| `datetime` | Valid `YYYY-MM-DDTHH:MM:SS[.ffffff](Z\|±HH:MM)` with explicit timezone, at most 35 characters |

Nulls and arbitrary nested values are unsupported. Text rejects literal `<`/`>`
and Unicode category C characters (including controls, surrogates, bidi controls),
except newline and tab. This deliberately simple plain-text development contract
also disallows literal angle-bracket mathematical notation. Entity-looking input
is literal text and is escaped again for PDF output. Aggregate canonical document
size is at most 1 MiB, independently of transport JSON-byte limits.

The claimed author and finalization time are **client-declared assertions**.
They do not authenticate a person, prove a signature or replace server-side
recipient eligibility. No new personnel identity mechanism was introduced.

## Integrity and exports

New rows record `document_schema_version=1` and `document_digest`, separately from
the existing `payload_digest` of canonical encrypted JWE bytes. The document hash
is SHA-256 of this canonical object:

```json
{
  "profile": "firedash-report-document-sha256-v1",
  "document": "THE COMPLETE VALIDATED DOCUMENT OBJECT, NOT A STRING",
  "attachments": [{"id": "UUID", "sha256": "LOWERCASE JPEG SHA256"}]
}
```

The attachments array in this hash wrapper is sorted lexically by UUID; document
arrays retain their order. Canonicalization uses sorted object keys, no whitespace,
ASCII JSON escaping (Python `json.dumps(sort_keys=True, separators=(',', ':'),
ensure_ascii=True, allow_nan=False)`). Strings are **not** Unicode-normalized;
decimal strings and timestamps retain their exact validated representations.
All numeric values are bounded integers, avoiding floating-point serialization
ambiguity. This is a named FireDash profile, not a claim of RFC 8785 compliance.
JSON whitespace/member order and JPEG transport ordering do not affect this hash;
document array ordering, content, display metadata and JPEG hashes do.
Re-encryption changes the transport digest and is still a conflicting replay.

`pdf.render_pdf(document, attachment_hashes=..., received_at=..., exported_at=...)`
validates and returns PDF bytes in memory. It uses ReportLab 4.5.1, A4 pages,
18 mm side/top margins, 20 mm bottom margin, wrapping paragraphs, splitting tables
with repeated column headings (including splitting a single oversized row), page
numbers and report ID footers. It prints all content, a numbered JPEG list with
captions/IDs/hashes, optional signature reference and document hash. Photos remain
separate JPEGs. Report type, claimed finalizing person and app finalization time
appear alongside explicitly labeled **server-observed** receipt/export times.
ReportLab [paragraphs](https://docs.reportlab.com/reportlab/userguide/ch6_paragraphs/)
and [tables](https://docs.reportlab.com/reportlab/userguide/ch7_tables/) supply layout;
untrusted strings are escaped before reaching the paragraph parser.
If column labels are too tall to repeat safely, the renderer prints their full
text in a flowing numbered legend and repeats compact column numbers in the table.

The bundled Vera font supports Western European text. Unavailable glyphs are
printed as `[U+XXXX]` (e.g. `[U+706B]`), preserving their identity rather than
silently dropping them. A broader font/shaping policy remains a localization
decision. PDFs contain no remote resources, active links, embedded photos or
client-supplied PDF actions. There is no HTML fetcher or host-font dependency.

`exports.render_submission_pdf(installation=..., submission_id=...)` and
`exports.read_submission_jpeg(..., attachment_id=...)` reuse persisted tablet
authorization and scope, reject expiry, check stored ciphertext and document
digests, and revalidate authenticated JSON/JPEGs before returning bytes. These
are local service entry points, **not new HTTP routes**. They neither write PDFs
nor mark a download. Export errors include `legacy_report_document` (409),
`report_integrity_failure` (409), `submission_expired` (410), and non-disclosing
404s for unknown/foreign resources. A retired private key must remain available
until all packages encrypted to it expire if exports are needed; exact replay
still requires no decryption key.

## Retention and attachment limits

The Phase 3.2 product decision supersedes the earlier provisional 72-hour default
and the architecture document's older retention table. New acceptance defaults to
168 hours. The effective deadline is always the minimum of:

1. Persisted acceptance deadline (which may be configured shorter).
2. Server receipt plus seven days.
3. First successful explicit download plus 48 hours, when recorded.

`first_download_at` is nullable. `services.record_successful_download(submission_pk=...,
completed_at=...)` is the exact future portal integration hook. Call it **after**
successful complete transmission of an explicit PDF or JPEG download, using the
server-observed completion timestamp; retain that timestamp for retries. The
optional omitted timestamp means immediate recording at server time. It rejects
future/pre-acceptance timestamps, never extends expiry and keeps the earliest
completion under concurrent or out-of-order callbacks. Rows are locked for this
update. Preview/render/read operations never invoke it. No existing code fabricates
download events, so current packages keep their initial deadline until this hook
is connected by the portal.

The portal slice must authorize recipient/session access itself before invoking
an export; these installation services do not grant recipient portal access.
The portal will need its own scoped package-loading path under recipient/session
authorization, reusing the validation and renderer without impersonating a tablet. It
must distinguish explicit downloads from previews/scanners, handle interrupted
responses without recording success, persist/retry real completion notifications,
and set private/no-store response headers. A renderer returning bytes or a 200
response being constructed is not a completed download. Server transmission
completion cannot prove that a browser saved the file; that product definition
must be stated in portal behavior. No token, session, HTTP delivery handler or
email flow is added here.

Status, exports and retryable cleanup enforce the same effective deadline, even
for an old or accidentally overlong persisted expiry. Existing 72-hour packages
are never extended. Cleanup retains the Phase 3.1 retry/orphan behavior and only
removes ciphertext; metadata tombstones remain for idempotency. Backup exclusion,
cleanup scheduling and minimal metadata retention still require deployment/privacy
decisions in their later scope. No server cleanup was executed.

Four JPEGs is restrictive for an illustrative report with an overview, multiple
damage/details, a reference image and a signature. Without claiming field research,
the reversible development default is **12 JPEGs**, configurable up to **32**.
Per JPEG remains 4 MiB / 16 million pixels; request default becomes 96 MiB to
accommodate the double base64 expansion of twelve 4 MiB images plus JSON.
Aggregate request size still applies even when individual maxima would permit
more data. Operator ceilings checked by Django are 128 MiB request, 1 MiB JSON,
8 MiB JPEG, 32 attachments, 32 million pixels and 168 hours retention. These remain
single bounded in-memory requests; worker memory/concurrency and upstream request
limits need measurement/alignment before deployment. No deployment limits changed.

## Safe development compatibility

Migration `0002_report_document_integrity` adds only hash/version/download metadata.
It does not decrypt, rewrite, extend or fabricate data for stored Phase 3.1 rows.
Those rows retain blank document hashes and null schema versions. Their exact
JWE replay and status continue working; their generic PDF export fails explicitly
with `legacy_report_document`. Newly submitted opaque JSON is rejected. Allow old
development packages to expire normally, then submit fresh finalized v1 documents
with new report/submission IDs. No conversion, forced cleanup or deletion is needed.
Old PDF-Mail APIs remain operational.

## Local reproduction and validation

This creates only a **synthetic disposable example PDF**, with demo hashes (not
real attachments), and prints its location. It uses no database or credentials:

```powershell
@'
import json, tempfile
from datetime import datetime, timezone
from pathlib import Path
from apps.report_transfer.pdf import render_pdf
document = json.loads(Path('docs/examples/report-document-v1.json').read_text())
hashes = {a['id']: 'a' * 64 for a in document['attachments']}
now = datetime.now(timezone.utc)
raw = render_pdf(document, attachment_hashes=hashes, received_at=now, exported_at=now)
with tempfile.NamedTemporaryFile(suffix='.pdf', prefix='firedash-demo-', delete=False) as f:
    f.write(raw)
    print(f.name)
'@ | .venv/Scripts/python.exe -
```

Focused PostgreSQL tests use generated credentials and disposable file storage:

```powershell
$env:DJANGO_SETTINGS_MODULE='config.settings.test'
.venv/Scripts/python.exe -m pytest -p no:cacheprovider apps/report_transfer/tests apps/outbound_mail/tests/test_report_admission.py apps/outbound_mail/tests/test_report_delivery.py apps/outbound_mail/tests/test_report_delivery_requests.py apps/tablets/tests/test_api.py apps/publications/tests/test_manifests.py -q
.venv/Scripts/python.exe manage.py check
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
.venv/Scripts/python.exe -m ruff check apps/report_transfer config/settings/base.py
```

Coverage includes all content/value kinds, arbitrary report types, malformed and
dangerous documents, attachment membership, hash determinism/content changes,
all-content encrypted submission with twelve JPEGs, A4 PDF extraction, tall-row
and multi-page overflow, scoped exports/tampering, replay/concurrent replay,
legacy compatibility, expiry and delayed/out-of-order download recording. Existing
authorization, recipient, encrypted-upload, cleanup, log confidentiality, signed
configuration and PDF-Mail regression tests are included. No full suite requested
or run.

Final validation: **212 passed in 37.44s** (115 Report Transfer tests and 97 existing
regression tests). Django checks reported no issues; migration checks reported
no changes; Ruff lint and formatting checks passed. Tests used the isolated local
PostgreSQL database and removed their disposable scratch storage on completion.

The synthetic JSON example rendered in memory as one A4 page (42,398 bytes with
fixed demo receipt/export time `2026-10-29T00:00:00+00:00`). With each demo JPEG
hash set to 64 lowercase `a` characters, its document hash is
`b9bc179eef8a79967e516c22f837209f0ad8af2bc3e8383242a936637269a40d`.
These are demonstration hashes, not submitted JPEGs or production data.

The remaining product work is approved client report content, broader localization,
actual workload measurements and the portal's authorization, explicit download
completion events and secure response delivery. No report-type backend work is
required for a new valid document.
