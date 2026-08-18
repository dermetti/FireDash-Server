# Administrator guide

This guide is for departmental application administrators. Server credentials,
systemd units, and cryptographic key files are operator responsibilities; see
[deployment.md](deployment.md) and [operations.md](operations.md).

## Scope and roles

Work only in departments and stations for which your account has the required
role. Administrative actions are audited. Sensitive actions can require a
fresh TOTP reauthentication; after it succeeds, return to the management page
and submit the action deliberately again.

## Stations, vehicles, and personnel

Create stations and vehicles in the owning department. Create personnel with
a single current HOME assignment. Transfers end the existing HOME assignment
and create the replacement; temporary assignments have an inclusive start and
exclusive end and remain historical after they expire. Keep commander
eligibility and verified email current.

Offboarding closes active assignments, removes commander eligibility and
verified-email access, and retains the historical person record. After the
department retention period, authorised anonymisation replaces personal
identifiers while retaining a stable historical record. Do not delete history
to work around retention.

## Reference data

Manage hydrants in the appropriate department and import them through the
provided validation workflow. Upload fire-plan PDFs only through the
quarantine and sanitisation workflow; an accepted file is the version used for
publication. Do not place PDFs directly into server storage.

## Canonical data imports

Imports use a deliberate **upload → preview → confirm** lifecycle. A preview
stores an exact source-file SHA-256 and shows adds, updates, deactivations,
unchanged rows, and bounded validation errors; it never changes canonical data
or starts publication work. Confirming applies that exact private staged source
once, atomically. If canonical data changed after preview, or staging is
missing or hash-mismatched, create a new preview instead of overwriting newer
work. **Import applied** means canonical data was committed; the existing
publication pipeline subsequently coalesces and publishes the affected scope.

Every data page provides **Add one** and **Import many**. Both create the same
one- or many-record `ImportBatch`; neither form writes a canonical record
directly. Hydrants accept a manual record or UTF-8 CSV, JSON, and GeoJSON
Point data in EPSG:4326 (`longitude`, then `latitude`). GeoJSON is preferred
for exchange. Merge leaves absent hydrants unchanged; **authoritative snapshot**
is batch-only, prominently shows its deactivation count, and affects only its
own department.

Personnel accepts a manual person or UTF-8 CSV/JSON batch. It is add/update
only: absence never offboards a person or ends an assignment. A new manual or
batch person requires an explicit home station.

Fire Plans and KLGV support either one PDF plus its metadata form or one ZIP
with `manifest.csv` and the declared PDFs. A Fire Plan uses a nonblank
`external_identifier` as its logical identity, or its exact address when no
External ID is available; a filename identifies a ZIP member only. Every PDF follows
quarantine, validation, and the sanitizer before it can become canonical. A
blank optional metadata field never erases curated metadata; coordinate
differences are shown in preview. KLGV is optional and remains disabled for
tablet distribution until its department feature is enabled. Download the
versioned plain-text templates from each import page; no spreadsheet or
proprietary GIS format is supported.

The upload page links directly to the exact UTF-8 schemas: `hydrants-v1.geojson`
(preferred) and `hydrants-v1.csv`; `personnel-v1.csv` and
`personnel-v1.json`; `fire-plans-manifest-v1.csv`; and
`klgv-plans-manifest-v1.csv`. PDF batch templates are `manifest.csv` files:
place the manifest and every declared PDF at the root of one ZIP. Fire Plan
columns are `external_identifier`, `filename`, `object_name`, `address`,
`postal_code`, `city`, `longitude`, `latitude`, and `action`; KLGV uses
`external_id`, `filename`, `title`, `category`, and `action`. `action` is
`upsert` or explicit `deactivate`; absence from a ZIP never deactivates a
document.

Raw uploads are private, bounded, and retained only for the configured staging
period; their contents are never copied into audit events.

## Publications

The Publications page is organised as:

1. **Scheduled updates** — coalesced source changes waiting for the nightly
   00:05 build. Use **Build & publish now** to expedite valid work.
2. **Building / publishing** — active work. Do not submit another build while
   a scope is already building.
3. **Attention / failures** — safe build error information and the previous
   known-good publication, where one exists. A failed update does not remove
   a working tablet dataset.
4. **Current publications** — active datasets and their human-visible
   attempt version.
5. **History** — superseded publications and useful failed or obsolete
   attempts, including scheduled/manual/bulk origin.

Normal successful, current builds publish automatically. Build versions are
immutable attempt identifiers, so gaps are expected after failed or obsolete
attempts. **Build & publish now** promotes existing work instead of creating a
second source change or duplicate job. The department bulk action does the
same for affected eligible scopes. Use **Rollback** only as an explicit
recovery action to a known-good historical publication.

## Tablets

Create a tablet and issue an adoption invitation. The iPad completes the
adoption protocol and receives an installation credential; do not record that
credential in tickets or audit notes. Departments set the maximum offline
lease (default seven days, minimum three days).

An ordinary successful check-in always records activity but renews the lease
only when 48 hours or less remain. The iPad's **Refresh tablet** action calls
the authenticated refresh endpoint to top up an active, authorised tablet to
the department maximum, then performs its existing configuration, manifest,
and conditional-download synchronisation. Refresh cannot reactivate stale,
expired, replaced, revoked, removed, or inactive installations.

Use the existing reactivation workflow for stale tablets. Revoke or remove a
lost tablet promptly; this prevents future use of its credential and data
delivery. Review the audit trail for these actions. For protocol details, see
[tablet-api.md](tablet-api.md).

System administrators can set the minimum supported FireDash application
version for API v1 from **API Compatibility** on the system dashboard. Leave
the value blank to permit all v1 applications. Setting a value makes older
installed apps receive **Upgrade Required** and should be used only after the
required release is available to affected tablets.
