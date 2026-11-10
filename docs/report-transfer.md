# Report Transfer

This is the authoritative feature guide for the supported beta reporting workflow.
Authenticated tablets submit encrypted ReportDocument JSON and JPEGs; recipients
receive controlled Magic-Link access to the report portal. The server generates
PDF downloads. Direct tablet PDF-mail is retired and unsupported.

[Tablet API](tablet-api.md#encrypted-report-transfer) is authoritative for tablet
request/response contracts. [Configuration](configuration.md#report-transfer),
[deployment](deployment.md#automatic-report-transfer-infrastructure-phase-36),
[operations](operations.md#report-transfer-expiry-and-recovery) and
[acceptance](acceptance.md#report-transfer-beta-acceptance) own their respective
operator procedures. Report Transfer has no publication/generation relationship
and never imports reports into canonical reference data.

## ReportDocument v1

The [complete synthetic example](examples/report-document-v1.json) illustrates
presentation structure, not a prescribed incident form. The validator is
[`documents.py`](../apps/report_transfer/documents.py). All members below are
required except the signature reference; unknown members are rejected. Arrays
retain client order. There is no report-type registry or type-specific renderer.

| Object | Structure and bounds |
| --- | --- |
| Document | `report_id` canonical UUID matching the transport; integer `schema_version: 1`; `report_type` (1 to 80 characters); `title` (1 to 200); `header`, `sections`, `attachments`, `finalization` |
| Header / fields | Ordered arrays of 0 to 100 `{key,label,value}` entries; label 1 to 200 characters; value is typed as below |
| Section | `{key,title,blocks}`; title 1 to 200; at most 50 sections, 50 blocks per section and 200 blocks overall |
| Text block | `{key,kind:"text",text}`; text 0 to 20,000 characters |
| Fields block | `{key,kind:"fields",fields}`; fields follow the header structure |
| Table block | `{key,kind:"table",columns,rows}`; 1 to 8 `{key,label}` columns (label 1 to 80); 0 to 500 rows, each an ordered typed-value array with exactly one cell per column; 4,000 data cells overall |
| Attachment references | `{key,kind:"attachment_refs",attachment_ids}`; 0 to 32 unique included JPEG UUIDs per block |
| Attachment display entry | `{id,title,caption}`; title 1 to 200; caption 0 to 2,000; ordered array of at most 32 unique UUIDs, exactly matching the transport JPEG set |
| Finalization | `{author:{id,display_name},finalized_at}`; author ID 1 to 128 and name 1 to 160 characters; optional `signature_attachment_id` references an included JPEG |

Keys match `[A-Za-z0-9._-]{1,64}`. Sections and blocks share one unique key
namespace per document. Header/fields keys and each table's column keys are
unique within their collections. Attachment references can recur across blocks.
Every JPEG needs display metadata even if no block references it. Paths, URLs,
arbitrary filenames and markup are not attachment metadata.

Typed values are exactly `{type,value}`:

| Type | Value |
| --- | --- |
| `text` | String of 0 to 4,000 characters |
| `integer` | JSON integer with absolute value at most 9,007,199,254,740,991; booleans excluded |
| `decimal` | ASCII decimal string at most 32 characters; optional minus, integer part without leading zeros, optional dot and digits; no exponent or leading plus |
| `boolean` | JSON boolean |
| `date` | Valid `YYYY-MM-DD` |
| `datetime` | Valid `YYYY-MM-DDTHH:MM:SS[.ffffff]` followed by `Z` or a signed `HH:MM` offset; at most 35 characters |

Null and arbitrary nested values are unsupported. Plain text rejects literal
angle brackets and Unicode category C characters except newline/tab. Strings
are escaped on display. Canonical document size is independently limited to
1 MiB, even outside transport validation.

The author, signature reference and `finalized_at` are client declarations.
They neither authenticate a person nor prove a signature. Server acceptance
(`ReportSubmission.created_at`) is a separate server-controlled time. Retention
uses server acceptance/download times, never the claimed finalization time.
The server records acceptance, not a `report_finalized` audit event.

## JWE and content integrity

The exact envelope/header fields are in the
[submission contract](tablet-api.md#post-apiv1tabletreport-submissions).
The five flattened JWE members use canonical unpadded Base64url. The protected
header binds submission/report/recipient UUIDs and installation/department/station
scope with `typ: firedash-report+jwe`, `package_version: 1`,
`schema_version: transport-json-v1`, `alg: RSA-OAEP-256`, `enc: A256GCM` and `kid`.
Key identifiers match `[A-Za-z0-9._-]{1,64}` and select dedicated RSA keys of at
least 3072 bits; configuration distributes the active public key as SPKI PEM.

For every new package generate a random 32-byte CEK and 12-byte IV. RSA wrapping
uses OAEP SHA-256, MGF1 SHA-256 and an empty label. AES-256-GCM authenticates the
ASCII bytes of the transmitted `protected` Base64url string as AAD. Separate the
16-byte tag from ciphertext. External AAD, compression, unprotected headers,
alternate algorithms and truncated tags are unsupported.

The decrypted UTF-8 JSON has exactly `report` and `attachments`. `report` has
`data` (Base64url exact UTF-8 ReportDocument bytes), decoded `size`, lowercase
hex `sha256` and `attachment_ids`. Each attachment has `id`, matching `report_id`,
`media_type: image/jpeg`, Base64url exact JPEG `data`, decoded `size` and `sha256`.
All three attachment sets (transport entries, transport references, document
metadata) must match exactly. Empty attachment lists are allowed.
Duplicate JSON keys, non-finite numbers, unknown fields, invalid UUIDs, hashes,
lengths, references or scope fail closed. Pillow verifies and fully decodes each
JPEG, enforcing byte and pixel ceilings plus JPEG start/end markers.

Default limits are 96 MiB encoded request, 1 MiB decoded report, 4 MiB per JPEG,
12 JPEGs and 16,000,000 pixels per image. The aggregate ceiling still applies;
double Base64 expansion matters. See [all limits](configuration.md#report-transfer).
Requests are whole bounded in-memory packages; there is no partial-upload resume.
Deployment must bound concurrency, request duration and total spool capacity.

`payload_digest` is SHA-256 of canonical encrypted-envelope JSON. Separately,
`document_digest` hashes `{profile:"firedash-report-document-sha256-v1",document,
attachments:[{id,sha256}]}`. Hash-wrapper attachments sort lexically by UUID;
document arrays retain order. Canonical bytes use Python-compatible sorted keys,
compact separators, ASCII JSON escaping and no non-finite numbers. Strings are
not Unicode-normalized; decimal strings and timestamps retain their exact
representation. This named FireDash profile is not RFC 8785. Whitespace/object
member order and JPEG transport order do not change the document digest;
document array order, metadata and attachment hashes do. Re-encryption changes
the transport digest and conflicts on replay.

## Tablet lifecycle and identities

The four active routes, without trailing slashes, are:

| Method | Path | Successful result |
| --- | --- | --- |
| POST | `/api/v1/tablet/report-submissions` | 201 new / 200 exact replay: `{submission_id,report_id,state,expires_at}` |
| GET | `/api/v1/tablet/report-submissions/<submission_id>/status` | 200 same metadata; READY or EXPIRED |
| POST | `/api/v1/tablet/report-transfers` | 201 new / 200 replay: `{transfer_id,status}` |
| POST | `/api/v1/tablet/report-transfers/<transfer_id>/revoke` | 200 REVOKED, including repeated revocation |

Bearer authentication proves the installation. Human operator authentication
remains the tablet app's responsibility. All four routes enforce app compatibility
and current active operational authorization, including lease and assignment.
Recipient authorization is rechecked for submission/replay and access requests.
Status and revocation disclose no foreign installation resources (404).

A report identity (`report_id`) and upload attempt identity (`submission_id`)
are unique within an installation. There is no revision/resubmission mechanism
for the same report identity. Finalize immutable content, persist IDs and exact
JWE durably, then upload automatically when connectivity permits. Recover a lost
response using exact replay or status; never regenerate encryption randomness or
IDs for that retry. Conflicting reuse is 409 `idempotency_conflict`. Expired
replay cannot restore content or extend expiry. Upload/replay must still match
the original encrypted station scope; status permits historical stations in the
same installation/department after reassignment.

Access issuance is a separate action, not finalization or upload. Synchronize all
locally finalized reports for the app-authenticated incident commander first.
Pending/failed/unconfirmed uploads block a new request; server-confirmed EXPIRED
history may remain local. The server cannot discover offline-only reports.
Persist a fresh `transfer_request_id` before intentionally requesting a link,
with only `recipient_personnel_id`; no selection list or email is accepted.

Under the installation lock shared with uploads, the server snapshots all READY,
unexpired v1 reports for that installation, department and recipient, including
historical stations. It verifies stored ciphertext/document integrity. Membership
is immutable: a concurrent upload falls wholly before or after the snapshot;
later reports need a new intentional request. Default maximum is 20 (effective
clamp 1 to 100); overflow is 409 `too_many_reports`, zero is 409
`no_reports_available`. No silent truncation or damaged-report omission occurs.

A transfer is an access generation, distinct from its reports. A new request UUID
atomically revokes preceding generations for that installation/recipient, including
sessions. Other pairs remain isolated. Replaying the saved UUID recovers the same
outcome without another token/email, refreshed membership or extended validity;
a different recipient conflicts. Validation failures before creation reserve no
request record. Default limits allow three requests per recipient/installation
per rolling minute and ten per installation per rolling hour. Replays do not
count again; revocation is not throttled.

Transfer statuses are PROCESSING, SENT, FAILED, UNKNOWN or REVOKED. FAILED,
UNKNOWN or a crash left PROCESSING never causes an automatic resend. Use a fresh
UUID only for an intentional replacement. Revocation stops grant access without
deleting report contents or extending retention. See the API guide for exact
error bodies/codes: malformed data 400, authorization 403, missing identities 404,
conflict 409, size 413, compatibility 426, rate limit 429, unavailable services 503.

## Recipient and portal access

The recipient must be an active, lifecycle-ACTIVE, incident-commander-eligible
person in the installation's department, with a verified valid commander email.
If department domain restriction is enabled, the normalized exact domain must
be approved; subdomains are not implicitly approved. Foreign, absent and
ineligible personnel IDs receive sanitized rejection. The server resolves this
state afresh; client mailbox values have no authority. A keyed HMAC fingerprint
binds the grant to the verified normalized mailbox without storing an address
snapshot. Changing the mailbox blocks that grant.

After committing the grant, the server rechecks authorization and uses the
verified department provider for one send attempt. Neutral mail contains only
the HTTPS access link, no report content, recipient name or attachments; the
recipient display name is generic. Only SENT grants permit login. Provider
acceptance is not proof of mailbox delivery. Fixed sanitized failure codes avoid
logging provider exception text. SMTP/provider verification and egress controls
remain prerequisites; see [configuration](configuration.md#outbound-mail-egress).

The link has 256 random bits; only SHA-256 of its token is stored. It is valid
for exactly 60 minutes from issuance and permits one login. GET
`/reports/access/<token>/` shows confirmation without consuming, revoking or
sending anything. Redemption requires a CSRF-protected POST to `redeem/` and
atomic consumption. At least one selected report must still be available.
A used link cannot log in again; within its original validity it can explicitly
revoke only its own generation via a two-step POST confirmation (`confirm=yes`).
That containment action works even if recipient eligibility has since disappeared.
An old consumed token can never revoke a replacement generation. GET requests
and rejected HEAD requests cause no access state changes.

Login flushes any existing Django session (including administrator identity),
creates a new database session and rotates CSRF. `ReportPortalSession` binds a
session-key hash to the grant with a 30-minute inactivity deadline. Active portal
sessions can outlive the link's 60 minutes while reports remain available. There
are no general recipient accounts or administrative rights. Logout is POST
`/reports/logout/`; confirmed portal revocation uses POST `/reports/revoke/`.

Every overview, detail, preview and download rechecks session binding/inactivity,
consumed grant, revocation, current recipient/domain/mailbox policy, historical
installation/department scope, membership and effective expiry. Issued grants
survive ordinary lease expiry, STALE state and station/vehicle reassignment.
Explicit installation revocation/replacement, `revoked_at`, disabled tablet,
INACTIVE/LOST/RETIRED tablet state or inactive department still deny access.
Historical scope is also checked against the authenticated JWE on payload load.
UUID knowledge alone never authorizes access. Expired reports disappear from
lists and direct access fails; remaining available members can still be used.

## Current portal and downloads

The German overview at `/reports/` shows compact report entries with title,
app-declared finalization time, photo count and availability deadline. Each has
a collapsed **Dateien herunterladen** list with one PDF and individual JPEG links,
and a separate **Details ansehen** action. It has no report-type/header-field
summary, receipt timestamp, thumbnails or automatic PDF generation. Entries sort
ascending by parsed timezone-aware finalization instant, then report UUID.
Displayed times follow the active Django timezone and include its zone label;
client finalization is not server-authenticated chronology.

The detail at `/reports/<report_id>/` dynamically renders ordered universal
header fields and text/fields/table sections without report-type branches. It
labels claimed author/finalization separately from server acceptance and shows
report UUID/document digest. A PDF download panel precedes the photo gallery.
Attachment-reference blocks are omitted from the display copy, with now-empty
sections removed; all attachments appear once in the ordered gallery with title,
caption, preview and JPEG download. The stored document remains unchanged.

PDFs at `.../pdf/` are generated in memory as generic A4 documents with wrapping,
splitting/repeated-heading tables, page/report footers, all finalized content,
attachment list/hashes, signature reference and document digest. Client assertions
and server receipt/export times are distinguished. Photos are separate JPEGs,
not embedded in the PDF. Text is escaped; there are no remote resources or
client PDF actions. Bundled Vera fonts support Western European text; unavailable
glyphs become `[U+XXXX]`. Broader localization remains a separate decision.

`.../photos/<attachment_id>/` downloads original validated JPEG bytes (which may
retain original metadata). `.../preview/` creates an in-memory thumbnail up to
640 by 480, applying EXIF orientation and stripping metadata. There are no
permanent PDF/preview files or public report-storage URLs. Download filenames are
stable ASCII: `report-{title-slug}-{report-UUID}.pdf` and
`report-{title-slug}-foto-{NN}-{photo-slug}-{report-UUID}-{attachment-UUID}.jpg`.
Slugs are bounded to 48/40 characters with `bericht`/`bild` fallbacks; full UUIDs
survive truncation. Photo numbers follow finalized attachment order.

Responses use private/no-store caching, `Referrer-Policy: strict-origin` (no bearer
path), restrictive local-only CSP, frame denial, nosniff and noindex/nofollow/noarchive.
No third-party resources or scripts are loaded. Portal cookies are Secure and
SameSite=Lax even in development; session cookies are HttpOnly. Use HTTPS locally
and preserve normal CSRF/Origin checks. Django/Gunicorn token filters and Nginx
access-route logging suppression protect bearer paths. Additional ingress, WAF,
APM, tracing and mail systems must also exclude token paths, bodies and mail
contents. Review the deployed proxy, not only repository templates.

## Retention and cleanup

Effective content expiry is always the earliest of persisted acceptance expiry,
server acceptance plus seven days, and earliest completed explicit download plus
48 hours. Configured shorter retention remains shorter. Repeated downloads never
extend it. Status and access enforce deadlines before scheduled deletion.

HTML/detail/thumbnail views never count as downloads. Recognized prefetch is
rejected; HEAD does not complete a download. PDF/JPEG streams reauthorize before
each 256 KiB chunk. Only normal complete WSGI/ASGI iterator exhaustion records
server completion; interrupted/closed or revoked streams do not reach that hook.
`X-Accel-Buffering: no` disables normal proxy buffering. Completion means handoff
to server/proxy transport, not proof the browser received or saved the file.
Already transmitted bytes cannot be recalled on revocation.

A private atomic `.receipt` journals actual completion before the database update,
with internal submission PK/time and optional download/transfer UUID and file type,
never content or tokens. Pending receipts already enforce the shorter deadline.
Cleanup retries recording them; duplicate recovery does not duplicate audit or
extend expiry. Old receipts without audit metadata remain usable for retention.
A crash between final handoff and receipt, or simultaneous spool/database failure,
cannot provide atomic delivery evidence; the original seven-day ceiling remains.

The canonical five-minute `fire-report-expiry.timer` runs
`expire_report_submissions` as `fire_backend` without the RSA credential. Cleanup
unlinks expired ciphertext before marking EXPIRED and clearing the reference;
missing files succeed, failed unlink retains the reference for retry. Writers
and orphan sweepers share a PostgreSQL advisory lock, protecting in-flight uploads.
Unreferenced generated `.jwe` files and `.receipt-tmp` files older than one hour
are swept. Failures produce a nonzero command result and safe counts. See
[operations](operations.md#report-transfer-expiry-and-recovery).

The private spool is outside publication/media/import/accepted-source trees;
overlap is rejected. Canonical deployment uses `fire_backend:fire_backend` mode
0700 directories and 0600 files, exclusive writes and fsync. Encrypted JWE is
stored on disk; database rows hold scope/identity, recipient FK, key version,
digests, timestamps, state and opaque file reference. Plaintext and CEKs exist
only during in-process validation/rendering, not in model fields/files. Python
cannot promise physical zeroization; review core dumps, swap and telemetry.

Metadata tombstones preserve idempotency after content expiry; transfer/session
metadata deletion horizons remain a privacy/operations decision. Schedule Django
`clearsessions` separately. Exclude spool and receipts from backups, versioning
and external volume/LXC snapshots. Protected credential backups must retain the
report keyring. Do not regenerate missing keys, reuse a retired version for new
material, or remove old keys while live encrypted reports need them. Exact replay
uses the stored digest and can survive key retirement; rendering needs the key.
Restoring a database alone cannot restore excluded report payloads or prove an
older download receipt; never fabricate content or extend deadlines for recovery.

## Compatibility and focused verification

Pre-ReportDocument development rows keep blank document hashes/null versions;
migration 0002 neither decrypts nor backfills invented documents/download events.
Exact replay and status remain compatible; exports reject `legacy_report_document`
(409), and new portal snapshots exclude those rows. Let old content expire and
submit fresh v1 content with fresh identities. Previously issued grants need no
rewrite. Request IDs reserved under the earlier selection-based contract cannot
be repurposed for recipient-based requests; use a fresh intentional request ID.
The retired `TabletReportDeliveryRequest` table/model remains inert for migration
compatibility; removing it requires a separate authorized task.

For authorized disposable testing, run `python -m tools.fake_ipad report-test`
with common `--server` and `--state-dir` flags after the report subcommand:

| Command | Purpose |
| --- | --- |
| `send --recipient-personnel-id UUID` | Persist/encrypt/upload one synthetic report and verify READY |
| `storm --recipient-personnel-id UUID` | Persist five independent synthetic reports with three JPEGs each before sequential upload; no mail |
| `status --submission-id UUID` / `retry --submission-id UUID` | Confirm IDs/status or replay exact durable ciphertext |
| `link --recipient-personnel-id UUID` | Read server status for local records, then intentionally request one email |
| `link --recipient-personnel-id UUID --transfer-request-id UUID` | Recover saved request outcome without a new generation/send |
| `revoke --transfer-id UUID` | Explicitly revoke that installation's grant |

These commands write server state; `link` can send real email. Use only an
authorized test installation/mailbox, never production. Existing adopted state
and datasets stay intact; the separate owner-protected `report-test/` directory
holds encrypted packages and minimal request/delivery metadata, no plaintext,
CEKs, private report keys, mailboxes, tokens or URLs. Protect Windows state with
private ACLs. Report commands reject identity overrides and `--save-plaintext`.
`link` accepts server-confirmed EXPIRED history but blocks missing, malformed,
failed or uncertain checks; encrypted queue records are never automatically deleted.

Functional coverage is in [Report Transfer tests](../apps/report_transfer/tests/),
[recipient tests](../apps/outbound_mail/tests/test_recipients.py),
[Fake iPad tests](../tools/fake_ipad/test_reports.py), and
[deployment probe tests](../tools/test_report_transfer_deployment.py).
Principal scenarios include exact/lost-response replay, cryptographic tampering,
cross-tenant denial, concurrent snapshot/upload, one-attempt mail, single-use
redemption, replacement/consumed-token races, session revocation, PDF/JPEG and
preview distinctions, interrupted streams, receipt recovery and cleanup retries.
These automated scenarios do not establish real Swift/JWE compatibility, browser
saving, mailbox delivery or native systemd isolation. Follow the concise
[beta acceptance checklist](acceptance.md#report-transfer-beta-acceptance) and
[development testing safety rules](development.md#windows-pytest-scratch-space).
