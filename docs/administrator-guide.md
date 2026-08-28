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

Personnel accepts a manual person or UTF-8 CSV batch. It is add/update
only: absence never offboards a person or ends an assignment. A new manual or
batch person requires an explicit home station.

Fire Plans and KLGV support either one PDF plus its metadata form or one ZIP
with a manifest and the declared PDFs. A Fire Plan ZIP uses
`fire-plans-manifest-v1.csv`; a KLGV ZIP uses `manifest.csv`. A Fire Plan uses a nonblank
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
`fire-plans-manifest-v1.csv`; and
`klgv-plans-manifest-v1.csv`. PDF batch ZIPs are manifest + PDF files:
place the Fire Plan manifest (`fire-plans-manifest-v1.csv`) or KLGV manifest
(`manifest.csv`) and every declared PDF at the root of one ZIP. Fire Plan
columns are `external_identifier`, `filename`, `object_name`, `address`,
`postal_code`, `city`, `longitude`, `latitude`, and `action`; KLGV uses
`external_identifier`, `filename`, `object_name`, `address`, `postal_code`,
`city`, `longitude`, `latitude`, and `action`. `action` is
`upsert` or explicit `deactivate`; absence from a ZIP never deactivates a
document.

Raw uploads are private, bounded, and retained only for the configured staging
period; their contents are never copied into audit events.

## Administrator accounts

Administrator authority is a lifecycle, not a profile flag. Department and
Station Administrator authority can be **Active**, **Suspended**, or
**Revoked**. Suspended authority has no effective access and can be
**Reinstated**; revocation is terminal for that authority record and a new
provisioning action is required if access is needed again. These transitions
are server-side, require an effective Department Administrator in the owning
department, and are audited.

Before suspending, revoking, or permanently removing a Department
Administrator, FireDash verifies that the department retains another effective
Department Administrator. It rejects an action that would leave the department
without one. Department Administrators are never given a Station scope; Station
Administrator authority is a separate, station-specific assignment.

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
recovery action to a known-good historical publication. Only a usable
superseded publication can be restored, and rollback is unavailable while a
newer attempt is staged or building.

The current publication is the scope's one authoritative distributed version.
Deleting it first atomically activates a usable predecessor; FireDash rejects
the deletion when there is no safe predecessor or newer work would make the
operation ambiguous. Deleting a successful historical version makes it
**Obsolete** rather than removing its permanent attempt identity. Artifact
cleanup is scheduled only after the transaction commits.

Maintenance keeps the current publication and the two newest usable rollback
predecessors in every scope. Older successful superseded publications become
**Obsolete** and their operational payloads can be removed. After the terminal
snapshot retention period, **Failed** and **Cancelled** attempts have only
their retained source snapshot purged; their terminal status and immutable
attempt identity do not change. Do not treat a retained history row as proof
that its artifact or source snapshot remains available.

## Overview and attention

Overview is an operational landing page, in this order: attention requiring
action, a small read-only operational state, then direct management
destinations. It is not an analytics dashboard or notification inbox. The
top-bar Attention indicator and Overview use the same request-cached,
authoritative result.

Department attention can show unassigned Tablets, stale installations, lost
Tablets, valid pending adoptions, and publication scopes that are not
published, have unpublished changes, are ready to publish, or have failed.
Each actionable publication scope counts once even if it has historical,
current, and candidate publication attempts. Scheduled and healthy building
work does not add attention. Station Administrators see only destinations valid
for their fixed station context; System Administrators see only reliable
system-level recovery signals. When there is no attention, Overview presents a
quiet positive status rather than an empty warning block.

## Tablets

A Tablet is the physical organisational asset (for example `FD-014`). It is
identified by its display name and optional asset number, and its **asset
state** is one of **Active**, **Inactive**, **Lost**, or **Retired**:

- **Inactive** — a known asset not currently in service (newly registered,
  stock/spare, temporarily withdrawn, or a recovered Lost tablet awaiting
  inspection). A valid current installation may remain attached and can sync a
  normal signed **empty** manifest so the app removes reference-data scope, but
  it cannot access operational datasets until the asset is activated again.
- **Active** — intentionally in operational service.
- **Lost** — the hardware cannot currently be accounted for. Recovery returns a
  Lost tablet to **Inactive**, never directly to Active.
- **Retired** — permanently withdrawn from service. Retired is normally terminal.

The Tablet asset is separate from its **installation** (the FireDash app
provisioned onto the device) and from installation **health** (Healthy or
Stale). A Tablet can be Active while its current installation is Stale. Assign
the tablet to an operational vehicle and issue an adoption invitation; the iPad
completes the adoption protocol and receives an installation credential. Do not
record that credential in tickets or audit notes. Then explicitly activate the
Tablet when it is ready for service. Departments set the maximum offline lease
(default seven days, minimum three days).

An ordinary successful check-in always records activity but renews the lease
only when 48 hours or less remain. A current Stale installation automatically
returns to Active when it reconnects with its durable credential and the Tablet
is Active with a valid assignment. The iPad's **Refresh tablet** action calls
the authenticated refresh endpoint to top up an active, authorised tablet to
the department maximum, then performs its existing configuration, manifest,
and conditional-download synchronisation. Refresh cannot reactivate stale,
expired, replaced, revoked, or inactive installations.

## Vehicle retirement and unassigned Tablet recovery

Retiring a Vehicle ends each open Tablet assignment with the recorded
`vehicle retired` reason. The Tablet is not unprovisioned or replaced: its
physical asset and installation history remain intact, while distribution
grants for the former assignment are revoked. The Tablet becomes explicitly
unassigned and appears in the Department Overview attention list.

Open the unassigned Tablet from Overview or Tablets, choose **Assign**, and
select an active Vehicle in the same department. This creates the new
assignment without inventing a replacement Vehicle or discarding the existing
installation. A retired Vehicle cannot receive a reassignment.

## Transfer a tablet

Use **Transfer** when the physical Tablet stays the same but its station or
vehicle assignment changes. A transfer changes assignment and therefore the
server-derived data scope, but it does **not** create a new installation:

    same Tablet
    same AppInstallation
    new assignment
    new server-derived data scope

Use **Assign** when there is currently no assignment. Department administrators
can transfer a tablet anywhere within the department; station administrators can
only transfer between vehicles in their own station.

## Re-provision FireDash

Use **Re-provision FireDash** when the **same physical tablet** needs a fresh
FireDash installation — for example after a factory reset, an app reinstall, or
lost/corrupted installation credentials. It does **not** change assignment and
must not be used to swap one physical iPad for another:

    same Tablet
    same assignment
    new AppInstallation

The re-provisioning workflow reuses the hardened adoption lifecycle:

1. An administrator chooses **Re-provision FireDash** and reauthenticates.
2. A fresh, one-time adoption invitation is created for the logical tablet.
3. The currently active installation **stays active and operational** while the
   invitation remains unused.
4. The new installation completes adoption with the invitation.
5. A successful adoption activates the new installation and marks the previous
   installation **REPLACED**. It preserves the physical Tablet asset state.
6. The previous installation's grants are revoked, and it receives
   `purge_provisioned_data=true` on its next status request.

There is never more than one active installation for a logical tablet. If the
invitation expires before adoption completes, the working tablet is unaffected;
generate a new invitation if needed.

## Physical replacement

A different physical iPad is a different Tablet asset. Retire the old Tablet and
create a new Tablet asset for the new device, then assign/transfer and provision
it as appropriate. Do **not** use **Re-provision FireDash** to overwrite a
physical asset's identity.

System administrators can set the minimum supported FireDash application
version for API v1 from **API Compatibility** on the system dashboard. Leave
the value blank to permit all v1 applications. Setting a value makes older
installed apps receive **Upgrade Required** and should be used only after the
required release is available to affected tablets.

## Locale and time display

Department **Locale and time display** is an administrator presentation policy.
It uses the supported locale and IANA timezone choices for Department pages;
Station Administrators inherit the policy of their Department. It does not
change stored timestamps, audit timestamps, signatures, API timestamps, lease
calculation, or any other protocol/security meaning: those remain UTC.
