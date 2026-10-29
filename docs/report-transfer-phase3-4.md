# Phase 3.4 — automatic report transfer and Fake iPad

## Current HTTP contract

This phase supersedes the selected-submission request in Phase 3.3. The authenticated
tablet sends only:

```http
POST /api/v1/tablet/report-transfers
Authorization: Bearer <installation credential>
Content-Type: application/json

{
  "transfer_request_id": "a06cbca6-ee33-492f-9af2-50ae30366f7b",
  "recipient_personnel_id": "2a47aada-2962-4325-af63-e582901841df"
}
```

`submission_ids`, email addresses and additional properties are rejected. The app
selects the currently authenticated operator's personnel UUID (`station_personnel`
→ `people[].id`). The Bearer authenticates the installation; it does **not**
independently prove the human operator's identity. Operator authentication remains
the real tablet app's responsibility.

The server rechecks installation, lease, active tablet, department and operational
assignment, then resolves the requested eligible IC and their verified mailbox
using the existing recipient/domain-policy services. Under the same installation
transaction lock as submission acceptance, it selects all READY ReportDocument-v1
reports for that exact installation, department and recipient whose effective
retention has not expired. Historical stations are preserved, not compared with
the tablet's current station. Ciphertext and document integrity are checked before
issuance. Row locks protect reads against concurrent expiry cleanup.

The resulting membership is immutable. A simultaneous upload is accepted either
before this snapshot and included, or afterward and excluded. A later upload
never expands an existing portal session. A fresh request UUID builds a fresh
snapshot and atomically revokes the preceding generation for this installation/IC,
including its sessions. Other installations and recipients remain isolated.

`REPORT_TRANSFER_MAX_REPORTS` defaults to **20**, with an enforced range of 1–100.
Overflow returns `409 too_many_reports`; nothing is silently truncated and no
email is attempted. Zero eligible reports returns `409 no_reports_available`.
An integrity failure fails closed; it does not silently omit a damaged report.

New successful issuance returns HTTP 201, replay HTTP 200, with only
`{"transfer_id":"UUID","status":"SENT"}` (or the existing neutral
PROCESSING/FAILED/UNKNOWN/REVOKED state). A previously reserved request UUID
recovers its transfer outcome, without sending again, generating a new token,
resetting expiry or refreshing membership. Reusing it for another recipient is
`409 idempotency_conflict`. Even after token consumption it cannot restore access.
Validation failures before creation do not reserve an idempotency record.

Existing Phase 3.3 grants/sessions and stored report packages need no data migration.
Clients must adopt the new request shape. A request UUID already reserved under the
old selection-based contract cannot be repurposed with the new fingerprint; use a
fresh UUID for an intentional new grant. Existing legacy document versions remain
stored and expire normally, but are not eligible for new v1 portal snapshots.

Existing 60-minute single-use tokens, GET confirmation, CSRF-protected redemption,
two-step consumed-token revocation of that generation only, 30-minute inactivity,
audit, mailbox binding and explicit security revocations remain in force. Issued
portal grants continue to survive normal lease expiry and station reassignment.
The submission status endpoint still requires current operational authorization,
but permits the same installation to confirm its historical submissions within
its department after reassignment. Upload authorization and encrypted scopes are
unchanged.

## Required real tablet behavior

The real tablet application is outside this repository. It must implement:

1. Finalize an immutable ReportDocument, retain it durably and enqueue delivery.
   Persist the exact encrypted JWE and submission UUID before sending it. Never
   regenerate ciphertext or IDs for a retry of a finalized report.
2. Synchronize automatically when connectivity becomes available. Keep failed or
   uncertain delivery records; a lost response is not grounds to discard a report.
   Confirm matching server IDs and READY through the status endpoint.
3. Provide one generic **Portalzugang anfordern** button for the app-authenticated
   IC. Before issuing a request, synchronize all locally finalized reports for that
   IC and show the outstanding count. If any remain pending/failed/unconfirmed,
   keep access issuance blocked and allow retry. Never silently email an incomplete
   known local batch.
4. Once all are confirmed READY, send the recipient-based request with a fresh UUID.
   Persist that request UUID before sending. Recover an uncertain result with the
   same UUID; use a new UUID only for an intentional new link/snapshot.
5. Keep finalization and delivery state separate. Requesting access does not finalize
   a report, re-upload it or mark another report delivered. Reports uploaded later
   require a new intentional access request to join the portal snapshot.

The server cannot detect reports that exist only on an offline tablet. There is
no endpoint claiming to check that condition. Local retention/archive policy for
the actual app remains an app concern; the test tool never automatically deletes
pending encrypted packages. Uploads remain bound to their original authenticated
scope; the tool never rewrites an old JWE after reassignment.

## Generic portal overview

Cards display the report title/type, all ordered universal header label/value
pairs, client-declared app finalization time, photo count, server receipt time,
availability deadline and **Bericht öffnen**. There is no incident field registry
or heuristic that guesses header keys. Synthetic incident number, time and location
are ordinary client-supplied header entries. All values use Django escaping.

Cards sort ascending by parsed, timezone-aware app finalization time, then by
report UUID. Equal instants expressed with different offsets compare correctly.
The date label explicitly says **Client-Angabe**; it is not server-authenticated
finalization evidence. Detail sections, thumbnails and individual generic PDF/JPEG
downloads are unchanged. Previews do not start retention; a completed explicit
server-side stream does, with the existing 48-hour ceiling within seven days.
Server handoff still cannot prove browser receipt or saving.

## Fake iPad commands and persistence

Run `python -m tools.fake_ipad report-test <command>` with the existing common
flags **after the report subcommand**, as with other Fake iPad commands:

| Command | Effect |
|---|---|
| `send --recipient-personnel-id UUID` | Finalize, encrypt, persist and submit one fictional report; verify READY |
| `storm --recipient-personnel-id UUID` | Finalize five independent reports, persist the whole batch, upload sequentially and verify all five READY; no email |
| `status --submission-id UUID` | Recheck a stored report's status and matching IDs |
| `retry --submission-id UUID` | Resubmit the exact stored JWE; accept 201 or exact 200 replay, reject 409 conflict; verify READY |
| `link --recipient-personnel-id UUID` | Check all locally known reports for that IC are READY, then explicitly request one generic link |
| `link --recipient-personnel-id UUID --transfer-request-id UUID` | Recover a previously persisted link request outcome, without issuing a fresh grant |
| `revoke --transfer-id UUID` | Explicitly revoke this installation's transfer |

`link` uses read-only status checks. If any check fails or returns non-READY, it
reports the outstanding count and sends no link request; use `retry` to synchronize
and run `link` again. A lost upload response can also be recovered by a READY
status check. A lost link response can be recovered with the saved request UUID.

`tools/fake_ipad/reports.py` contains the focused, Django-independent fixture/JWE
producer and queue. It reuses FakeIPadClient configuration, ApiClient transport,
DeviceState and secure-write/output conventions, and does not change adopted
credentials, app identity, manifests or dataset artifacts. Report commands reject
app identity overrides and `--save-plaintext`.

State is confined to `<state-dir>/report-test/`: owner-only directory/file modes
where supported, atomic existing secure writes. Windows permissions follow the
existing Fake iPad state convention; protect the selected state root with the
operator's private ACL. Submission files contain only the flattened encrypted JWE,
UUIDs, server origin, TEST identifier and delivery status/expiry. `requests/` stores
minimal link-request IDs and scope for outcome recovery. No plaintext document,
decoded photograph, CEK, report private key, mailbox, token or URL is saved.

The generator first obtains authenticated, enabled Report Transfer v1 configuration
and a valid RSA public key (at least 3072 bits). If this initial configuration fetch
fails, no synthetic report has yet been finalized. Once generated, the complete
encrypted batch is durable before the first submission attempt; connectivity
failure cannot discard it. There is no background process or implicit server write.

Each independent fixture has fresh report/submission/attachment UUIDs, type
`incident`, TEST / SIMULATED titles and fictional authors, ordered incident
number/time/location headers, situation text, structured measures, a vehicle/crew
table, attachment references, captions and three genuine Pillow JPEGs (480×320).
The five consecutive incidents are TEST-001 through TEST-005, at 14:08, 14:47,
15:32, 16:16 and 17:05 UTC on the fixture date; finalization is claimed 25 minutes
later. These are synthetic client declarations, not real incidents.

The producer independently builds the exact five-member flattened JWE, canonical
JSON, unpadded base64url, lengths/SHA-256/attachment manifests, authenticated scope
header, fresh 32-byte CEK/12-byte IV, RSA-OAEP-256 wrapping and AES-256-GCM with
the encoded protected header as AAD. Advertised count, byte and pixel limits are
checked. It imports neither Django nor server document/crypto builders.

Verbose report HTTP logging omits request and response bodies entirely. Recursive
output redaction also covers all five JWE fields and mailbox/link fields. CLI
results include only UUIDs, TEST identifiers, attachment counts, HTTP status,
READY/expiry and neutral transfer status. Server error detail and provider messages
are not echoed by report requests. Existing nine-dataset verification is retained.

## Manual testserver acceptance (not executed during implementation)

These are **manual, state-writing examples** for an authorized disposable test
installation and test mailbox. `storm`, `send`, `retry`, `link` and `revoke` perform
server writes; `link` can send a real email. Do not run them against production.
Use the existing adopted state root; the report commands create only their separate
subdirectory. No adoption, credential replacement or dataset changes are required.

```powershell
$python = '.venv/Scripts/python.exe'
$server = 'https://your-authorized-testserver.example'
$state = 'C:\private\existing-fake-ipad-test-state'
$ic = 'REPLACE-WITH-AUTHORIZED-TEST-PERSONNEL-UUID'

& $python -m tools.fake_ipad report-test storm --recipient-personnel-id $ic --server $server --state-dir $state --json
# Expect five distinct 201/READY reports, each with three photographs; no email.

$submission = 'COPY-A-SUBMISSION-UUID-FROM-THE-RESULT'
& $python -m tools.fake_ipad report-test status --submission-id $submission --server $server --state-dir $state --json
& $python -m tools.fake_ipad report-test retry --submission-id $submission --server $server --state-dir $state --json
# Exact retry: HTTP 200, READY, same IDs. No duplicate report.

& $python -m tools.fake_ipad report-test link --recipient-personnel-id $ic --server $server --state-dir $state --json
# Explicit email request. Save returned transfer ID; no token or link is printed.

# If that HTTP response was lost, use the UUID filename from report-test\requests:
$request = 'COPY-THE-STORED-REQUEST-UUID'
& $python -m tools.fake_ipad report-test link --recipient-personnel-id $ic --transfer-request-id $request --server $server --state-dir $state --json
```

Acceptance checklist:

1. Start with a clean, authorized disposable report context; existing eligible
   reports are intentionally included too. Run `storm`; verify five READY IDs and
   three photos each. Simulate connectivity loss if desired: encrypted queue stays
   present, pending reports block `link`, and explicit retries recover them.
2. Run `link` explicitly. Receive one neutral email at the server-verified test
   mailbox. GET the link: only confirmation, no redemption. Click **Sicher anmelden**.
3. Verify five distinguishable chronologically ordered cards (TEST number, time,
   location, client-declared finalization and three photos). Open every report,
   preview its photographs, download each PDF and each individual JPEG for manual
   SyBOS entry. Check previews do not shorten expiry; a completed download does.
4. Reopen the used email link; confirm the used-link warning and two-step revoke
   option. Redeeming again must fail. Confirm its revoke; the old session/downloads
   must immediately fail.
5. Run `link` with a new UUID (default). Explicitly redeem the new email; access
   works again. The old consumed token cannot revoke this new generation.
6. Optionally `send` one later report: the existing portal excludes it. Replaying
   the old request ID still excludes it and sends no email. A fresh `link` includes
   it and invalidates the old session.
7. Exercise `report-test revoke --transfer-id UUID` explicitly and verify immediate
   portal denial. Do not run live cleanup as part of this implementation task.

## Local validation

Only mocked/in-process HTTP, mocked outbound mail, isolated test PostgreSQL and
disposable storage are used. The independent producer is accepted by the actual
submission parser, crypto validation and database services in integration tests.
Validation on 2026-10-29:

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings.test'
.venv/Scripts/python.exe -m pytest -p no:cacheprovider apps/report_transfer/tests tools/fake_ipad/test_reports.py tools/fake_ipad/test_cli.py tools/fake_ipad/test_output.py tools/fake_ipad/test_generations.py tools/test_fake_ipad.py tools/test_fake_ipad_server_contract.py apps/outbound_mail/tests/test_report_admission.py apps/outbound_mail/tests/test_report_delivery.py apps/outbound_mail/tests/test_report_delivery_requests.py apps/tablets/tests/test_api.py -q --tb=short
.venv/Scripts/python.exe manage.py check
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run
.venv/Scripts/ruff.exe check apps/report_transfer tools/fake_ipad tools/test_fake_ipad.py tools/test_fake_ipad_server_contract.py config/settings/base.py
.venv/Scripts/ruff.exe format --check apps/report_transfer tools/fake_ipad/cli.py tools/fake_ipad/reports.py tools/fake_ipad/test_reports.py tools/fake_ipad/transport.py tools/fake_ipad/output.py config/settings/base.py
```

- **625 passed, 1 skipped**, 153.24 seconds. The existing POSIX-only owner-mode
  test is skipped on Windows; Windows ACL enforcement is not claimed by that test.
- The 27 new report-tooling cases cover real in-process encrypted server acceptance,
  independent documents/three JPEGs, exact 200 replay/409 conflict, lost response,
  pending-queue gating, five-report snapshot/replacement/consumed-token behavior,
  all five PDFs and fifteen JPEGs plus previews, limits, arbitrary header keys/type,
  chronology/offset/tie handling, CLI dispatch/redaction and both concurrent
  upload/snapshot orderings. Existing installation/recipient isolation, audit,
  cryptography, TTL, portal sessions and PDF-Mail tests pass with the updated contract.
- Django check: zero issues. Migration check: no changes detected; no migration needed.
- Ruff checks and formatting pass. Read-only Git diff whitespace check passes.
- CLI help checked locally; disposable `.test-tmp` storage removed by test hooks.
- No full suite, live traffic, emails, cleanup jobs, deployment, Git writes or
  adopted Fake-iPad state/dataset modifications were performed. The manual
  testserver checklist above remains explicitly unexecuted.
