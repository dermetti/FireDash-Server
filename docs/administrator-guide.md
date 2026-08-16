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
