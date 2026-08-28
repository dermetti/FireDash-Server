# FireDash Backlog Refactor Plan

**Basis:** Review of the current Bugs & Essential Feature Backlog and the supplied repository code graph.

**Goal:** Turn the mixed bug/feature/UI backlog into a small number of coherent refactor stages, with backend semantics established before UI work where required.

**Status (Phase 5 candidate):** Stages 1--4 are implemented regression
baselines, not active feature work. Stage 5 local closure is complete; the
remaining gate is deployment of one user-created, pushed exact SHA followed by
recorded live acceptance. This document retains earlier scope as a decision
record and names genuinely deferred work instead of silently deleting it.

---

## 1. Backlog review and normalization

The backlog currently mixes four categories:

1. Already implemented items that should become regression checks.
2. UI consistency defects.
3. Missing domain capabilities.
4. Policy/data-contract changes that require backend design before UI work.

These should not be implemented as one flat checklist.

### Implemented / retained as regression coverage

Remove these from the active backlog after live verification and retain focused regression coverage instead:

- Tablet list nested `.table-responsive` / scroll-container regression.
- System Administrator audit-log list UI.
- System Administrator API Compatibility administration UI.
- Department Administrator station-scope switching in normal navigation/context.
- Department/System bounded audit lists.
- Existing per-department Tablet authorization lease setting UI.
- Existing personnel retention setting UI.

### Implemented management UI baseline

- Bootstrap form styling audit across remaining forms.
- Consistent HTMX/Bootstrap modal use for short create/edit actions.
- Data Hub icons.
- Default lists hide historical/inactive/problem states according to the approved domain policy.
- Column-relevant server-side filters with HTMX refresh.
- Remove redundant Filter buttons where live filtering is used; retain Reset.
- Back-navigation on pages not directly reachable from sidebar/Data Hub.
- Consistent row Actions dropdowns and record-name detail links.
- Audit-event detail pages.
- Station status/context presentation and detail-page spacing.

### Implemented domain capabilities

- Station/Vehicle and Personnel imports with scoped station resolution and
  atomic final Apply.
- Administrator lifecycle, detail workflow, and last-effective-Department-
  Administrator protection.
- Concurrent-safe optional Tablet asset-number allocation.
- Department locale/time presentation policy.
- Publication rollback, active deletion, bounded retention, and scope-level
  Overview attention.
- Hydrant address fields and distributed metadata.
- KLGV canonical and distributed manifest metadata.
- Personnel create workflow with email and commander eligibility.

### Approved lifecycle visibility policy

- **LOST Tablets in the default list.** The default Tablet list shows only Active and Inactive physical assets. LOST and RETIRED assets are available only through the explicit Asset State filter.

### Resolved policy and genuinely deferred work

- **Revoked/replaced installations visibility (resolved).** Installation history is
  detail-only and is not mixed with Tablet asset state.
- **Retired Vehicle with assigned Tablet (resolved).** Retirement ends the
  assignment, preserves Tablet/install history, revokes former distribution,
  creates explicit unassigned attention, and supports reassignment.
- **Publication deletion/rollback (resolved).** These are locked, transactional,
  audited service operations; current scope state is authoritative.
- **Timezone/local time (resolved).** Department policy is presentation-only;
  protocol and security timestamps stay UTC.
- **Stale-installation timing (deferred).** No per-department setting exists;
  do not expose one without a separately approved authoritative policy.
- **Fire Plan v2 delivery architecture (deferred).** The current monolithic
  complete artifact remains the v1 contract; per-document sync is future work.

---

## 2. Refactor principles

Implement by subsystem, not page-by-page.

Code ownership visible in the supplied graph:

- `apps/portal/` — administration shell, Overview, Department/System administration.
- `apps/authorization/` — memberships, scopes, compatibility and lease policy.
- `apps/organizations/` — Department, Station and Vehicle canonical state.
- `apps/assignments/` — Tablet/personnel assignment semantics.
- `apps/tablets/` — Tablet asset/install lifecycle and management UI.
- `apps/personnel/` — personnel lifecycle, retention and canonical fields.
- `apps/reference_data/` — Hydrants and document-backed canonical data.
- `apps/ingestion/` — import forms, parsers, validation, review and apply.
- `apps/publications/` — publication state, jobs, activation, manifests and artifacts.
- `apps/audit/` — append-only audit source of truth.

General rules:

- Preserve server-side authorization and department/station scoping.
- Keep lists bounded, deterministic, server-filtered and paginated.
- Use HTMX for list filtering and short modal interactions, not business-state ownership.
- Use record names/identifiers as detail links; Actions dropdowns contain mutations.
- Keep delete actions detail-page-only where required.
- Keep Tablet asset lifecycle separate from AppInstallation lifecycle.
- Do not alter `/api/v1` meanings for additive optional distributed metadata.
- Publication, assignment and security changes must be service-layer operations with audit and transaction boundaries.

---

# 3. Recommended implementation stages

The following Stage 1--4 detail is retained as the implemented decision and
regression record. It is not an active implementation checklist.

## Stage 1 — Management UI and list consistency (implemented)

**Purpose:** Remove remaining cross-application UI inconsistencies before adding more workflows.

- Audit remaining forms for Bootstrap labels, controls, selects, help text and validation.
- Normalize short create/edit interactions to the proven HTMX/Bootstrap modal pattern.
- Add “back to scope” navigation on workflow pages not reachable from sidebar/Data Hub.
- Standardize list behavior:
  - operational rows by default;
  - historical/problem states only through explicit filters according to current domain policy;
  - server-side filters appropriate to displayed columns;
  - HTMX refresh with a short debounce;
  - result count and pagination;
  - Actions dropdowns.
- Add missing Station filters, address display, “Short Code” naming and Station status/context.
- Add Vehicle Active/Retired filtering and correct action presentation on Station detail.
- Add Data Hub icons.
- Add AuditEvent detail pages while keeping audit immutable and safe.
- Verify the already-fixed Tablet page-flow/pagination regression remains fixed.

**Acceptance:** no hidden inline forms where modals are intended, no unbounded lists, no redundant “View details” action where the primary identifier links to detail, and no cross-department leakage through filters/HTMX.

---

## Stage 2 — Canonical data and import pipeline refactor (implemented)

**Purpose:** Make canonical data entry/import coherent across Stations, Vehicles, Personnel, Hydrants and KLGV.

Build on the existing `apps/ingestion` parser/review/apply pipeline instead of creating parallel import systems.

### Stations and Vehicles

- Add canonical Station street/house-number fields if not already represented suitably.
- Add Station batch import.
- Support Vehicle rows in the same Station/Vehicle workflow or a tightly coupled domain-specific workflow.
- Resolve station references by normalized Short Code and full station name.
- If a Vehicle references a missing Station, surface it during preview/review and allow creation/confirmation before apply.
- Keep apply atomic where practical and audit canonical changes.

### Personnel

- Keep import mode in the upload form, never inside CSV rows.
- Explain Upsert/Merge semantics with permanent help text.
- Use `home_station`, not generic `station`.
- Resolve home station tolerantly from Short Code or full name.
- Add Create Person modal with email and commander eligibility.
- Explain Offboard versus Anonymize before mutation.

### Hydrants

- Add street/house-number address data to canonical model/import/edit/publication paths.
- Preserve geometry/coordinate semantics.
- Update filters/detail around the new address field.

### KLGV

Bring the KLGV canonical/document manifest to:

- `id`
- `external_identifier`
- `object_name`
- `address`
- `postal_code`
- `city`
- `longitude`
- `latitude`
- `sha256`
- `page_count`
- `path`

Treat optional new metadata as additive and keep deterministic artifact validation.

**Acceptance:** unresolved stations fail visibly before apply; ambiguous station matching never guesses; failed imports do not partially create canonical resources; canonical changes trigger correct publication dirty/rebuild behavior; Hydrant/KLGV contract changes have ingestion/publication tests.

---

## Stage 3 — Administrator, Tablet assignment and policy lifecycle (implemented)

**Purpose:** Refactor authority and lifecycle rules that currently leak through ad-hoc UI controls.

### Administrator Accounts

Implement a real membership lifecycle:

- Active
- Suspended
- Revoked
- Reinstate from Suspended

Department Administrator list:

- Administrator — screen name linking to detail.
- Email.
- Access / Scope — `Department` or Station Short Code.
- Status.
- Actions.

Rules:

- Department Admin Station scope displays **Not applicable**.
- Remove “Grant station” from Department Admin controls.
- Separate “Provision Department Admin” and “Provision Station Admin” modal actions.
- Add administrator detail page.
- Permanent deletion remains detail-page-only.
- Prevent revocation/deletion that would leave a Department without any effective Department Administrator.
- Suspend and revoke are distinct, audited server-side operations.

### Tablet assignment/orphan handling

When a Vehicle is retired or permanently deleted:

- do not revoke/unprovision the Tablet;
- do not create an invisible placeholder Vehicle;
- move the Tablet into an explicit unassigned/orphaned assignment condition;
- preserve logical Tablet/install state where allowed by existing lifecycle rules;
- block distribution that requires an operational assignment if existing security semantics require it;
- surface actionable Overview attention;
- provide a supported reassignment path.

### Tablet list/lifecycle cleanup

- Retain regression coverage for the approved default-list policy: show Active and Inactive only; expose LOST and RETIRED through the Asset State filter.
- Ensure Pending and other intended operational states are represented correctly.
- Keep revoked/replaced installation history in Tablet detail/history or an explicit installation filter, not Tablet asset state.

### Optional asset-number generation

- Optional configuration.
- Manual entry remains supported unless deliberately disabled.
- Generated values obey Department uniqueness.
- Allocation is concurrency-safe using database-backed locking/sequence semantics.

### Department policy/settings

Use full-width settings cards, one per row, each with its own Apply action:

- Tablet authorization: max offline lease, 3–365 days.
- Personnel retention.
- Locale/time display.
- Publication retention is deferred to Stage 4, where storage and lifecycle enforcement are delivered together.
- Stale/cleanup policy remains deferred unless an authoritative per-department backend setting is introduced.

**Acceptance:** last effective Department Admin cannot be removed; suspended/revoked access cannot continue; no Department Admin Station-scope mutation survives; Vehicle retirement never silently destroys Tablet provisioning; orphaned Tablets are explicit/auditable/recoverable; asset-number allocation passes concurrent-create tests.

---

## Stage 4 — Publication lifecycle, retention and operational attention (implemented)

**Purpose:** Treat Publications as one coherent versioned lifecycle rather than disconnected lists.

### Publication list/detail

Consolidate into one bounded/filterable view:

- current/pending/problem publications visible by default as appropriate;
- superseded versions hidden unless explicitly filtered;
- first dataset/scope column links to detail;
- detail title is the human-readable dataset Scope;
- version is secondary metadata;
- Actions are a dropdown.

State-aware actions:

- Build update when needed.
- Roll back to previous good version from current where valid, even if an update is pending.
- Roll back to a selected superseded version where valid.
- Delete only from detail.

### Rollback/delete semantics

Implement in `apps/publications` services first.

Invariants:

- one authoritative active publication per scope;
- rollback is an audited atomic activation change;
- pending/building updates do not make rollback ambiguous;
- deleting the active publication activates the previous valid/good version first when one exists;
- if no safe predecessor exists, reject deletion or require an explicit non-distributed outcome;
- grants/manifests follow the authoritative active publication through existing mechanics;
- artifact cleanup occurs only after transaction commit;
- retention cleanup cannot delete protected/minimum-retained/current publications.

### Publication retention

Implement:

- minimum superseded publications to keep, at least 1;
- optional age retention for older unprotected superseded publications;
- empty age limit = unlimited;
- destructive warning;
- bounded cleanup using the same invariants as manual deletion.

### Overview correctness

- Count distinct actionable publication scopes.
- Avoid duplicate attention from current/pending/failed rows for one scope.
- Explain exactly why each scope needs attention.
- Link to the affected scope/detail.
- Add orphaned/unassigned Tablet attention from Stage 3.

**Acceptance:** rollback works with/without pending updates; active delete cannot leave a dangling scope/artifact; retention never removes current/protected/minimum-kept publications; Overview count matches distinct actionable scopes.

---

## Stage 5 — Final hardening, documentation and Alpha acceptance (local closure complete; exact-SHA live gate pending)

**Purpose:** Close the refactor without policy drift between code, PRD and backlog.

- Reconcile PRD/backlog/docs with approved changes:
  - desktop-first management table/page-flow rule;
  - administrator lifecycle;
  - orphaned Tablet assignment behavior;
  - Station/Vehicle import semantics;
  - Hydrant/KLGV fields;
  - publication rollback/delete/retention;
  - locale/time-display policy.
- Completed locally: documentation reconciliation, focused subsystem tests, one
  final full regression pass, and deployability/static/migration checks.
- Pending post-commit: deploy one exact pushed SHA to the LXC, record verifier
  evidence, perform Department/Station/System Administrator acceptance, and
  finish the deferred Tablet lifecycle Alpha exercise.

**Final acceptance focus:** role isolation; import atomicity; Tablet assignment/security invariants; publication rollback/retention safety; no backend incident/intervention storage; no breaking `/api/v1` changes from additive reference metadata; audit continuity for destructive/admin actions.

---

# 4. Recommended priority order

1. **Stage 1 — UI/list consistency**
2. **Stage 2 — Canonical data/imports**
3. **Stage 3 — Administrator/assignment/policy lifecycle**
4. **Stage 4 — Publication lifecycle/retention/attention**
5. **Stage 5 — Hardening/acceptance**

This order establishes data shape before lifecycle/policy work, and lifecycle/policy before the highest-risk publication changes.

---

# 5. Keep out of this refactor

Unless separately approved:

- SPA/React/Vue.
- Generic notification inbox.
- System Health/backups/storage monitoring.
- New Tablet API crypto.
- New publication crypto.
- Backend incident/intervention storage.
- Fake Tablet `LOCKED` state.
- Hidden/fake Vehicle for orphaned Tablets.
- Client-breaking `/api/v1` field renames/type changes.
- Arbitrary timezone conversion of signed/protocol timestamps.
- Unbounded audit/import/publication lists.

---

# 6. Definition of done for each stage

A stage is complete only when:

- authoritative backend semantics exist first for non-cosmetic changes;
- authorization/scoping is enforced server-side;
- mutations remain POST/CSRF protected;
- relevant actions are audited;
- lists remain bounded;
- focused tests cover success, invalid state and cross-scope denial;
- migrations are explicit and justified;
- publication/assignment side effects are regression-tested where touched;
- FireDash remains deployable;
- deferred items are reported explicitly instead of silently skipped.
