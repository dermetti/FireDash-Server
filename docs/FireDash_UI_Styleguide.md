# FireDash UI Styleguide

**Purpose:** A practical design and implementation guide for future FireDash Server UI additions, especially new Data Hub modules and administration pages.

**Primary UI stack:** Django templates → Bootstrap → HTMX → Alpine.js for small local visual state → minimal custom JavaScript only when unavoidable.

---

# 1. Design principles

FireDash is an operational administration application, not a marketing site and not a decorative dashboard.

Every page should feel:

- calm;
- operational;
- predictable;
- sparse rather than crowded;
- understandable without training;
- desktop-browser-first;
- progressively enhanced rather than SPA-like.

Every page should help an administrator answer:

1. Where am I?
2. What is the current state?
3. What requires attention?
4. What can I do here?
5. What data already exists?
6. What will happen if I change something?

**Operational clarity is more important than maximum information density.**

Do not introduce visual complexity simply because Bootstrap offers a component for it.

---

# 2. Application shell

Authenticated administration uses one shared shell:

```text
┌────────────────────────────────────────────────────────────┐
│ Sticky top bar                                             │
├────────────────┬───────────────────────────────────────────┤
│ Sidebar        │ Main content                              │
│                │                                           │
│                │                                           │
└────────────────┴───────────────────────────────────────────┘
```

## Desktop

Desktop is the primary target.

- Persistent sidebar.
- Sticky top bar.
- Main page owns vertical scrolling.
- Avoid nested scrolling regions.
- Use the available width without making pages visually dense.
- Prefer vertical stacking over many side-by-side cards.

## Tablet/mobile

Tablet/mobile support remains functional but secondary.

- Sidebar may become Bootstrap Offcanvas.
- Use the same navigation definition as desktop.
- Do not create separate mobile templates.
- Do not damage desktop usability merely to avoid every possible horizontal wrap.

---

# 3. Global page anatomy

Management pages should normally follow this vertical structure:

```text
Breadcrumb / context
Page title
Short description

Feedback

Status & Context       ← only if meaningful

Actions

Existing Data

Contextual resource actions
```

Do not add empty Status cards merely for consistency.

## Page header

Every management page starts with:

- breadcrumb or scope/context;
- one clear page title;
- one short explanatory sentence.

Avoid large hero banners.

## Feedback

Mutation feedback appears immediately below the page header.

Use Bootstrap alerts:

- `alert-success`
- `alert-warning`
- `alert-danger`
- `alert-info`

Feedback should state:

- what happened;
- why, when relevant;
- what the administrator should do next.

Do not rely on toast notifications for important operational feedback.

---

# 4. Typography and information hierarchy

Use Bootstrap’s normal type scale and semantic HTML.

Recommended hierarchy:

```text
h1  Page title
h2  Major page section
h3  Subsection / detail group
body Operational content
small / text-body-secondary Secondary identifiers or metadata
```

Rules:

- One `h1` per page.
- Avoid oversized marketing-style headings.
- Prefer concise labels over prose-heavy cards.
- Secondary IDs, versions and timestamps should visually support—not compete with—the primary record name.
- Use bold text sparingly for state or labels, not whole paragraphs.

---

# 5. Spacing and layout

Prefer a clear vertical rhythm.

Recommended Bootstrap patterns:

```html
<div class="mb-4">...</div>
<div class="mb-3">...</div>
<div class="d-flex gap-2 flex-wrap">...</div>
```

General rules:

- Major page sections should have visible vertical separation.
- Buttons in the same action group should have consistent gaps.
- Avoid tightly packed controls.
- Avoid large empty decorative space.
- Avoid multiple unrelated cards in the same row unless comparison is genuinely useful.
- Settings cards should normally span the full content width and appear one per row.

---

# 6. Cards

Cards are for coherent groups of related information, not every piece of content.

Use cards for:

- Data Hub module entry points;
- read-only status/context groups;
- settings groups;
- focused operational summaries.

Do not wrap ordinary management tables in cards merely to create a box.

## Card rules

A card should normally contain:

```text
Title
Short description
Relevant status/context
One clear destination/action
```

Avoid:

- multiple unrelated button rows;
- deeply nested cards;
- large decorative headers;
- card-within-card layouts.

---

# 7. Data Hub style

The Data Hub is the visual gateway to distributed/reference data.

It is **not a CRUD dashboard**.

Typical modules:

- Hydrants
- Personnel
- Fire Plans
- KLGV Plans
- future distributed datasets

## 7.1 Card anatomy

Each Data Hub card should follow the same structure:

```text
[Icon] Module name

Short one- or two-line description.

Active records     428
Publication        v17 · Current
Last changed       Today · 14:42

The whole card is the module link.
```

Suggested HTML structure:

```html
<div class="card h-100">
  <div class="card-body d-flex flex-column">
    <div class="d-flex align-items-center gap-3 mb-3">
      <div class="firedash-module-icon" aria-hidden="true">
        <!-- vendored/local icon -->
      </div>
      <h2 class="h5 mb-0">Hydrants</h2>
    </div>

    <p class="text-body-secondary">
      Water supply reference points distributed to operational tablets.
    </p>

    <dl class="row small mb-4">
      <dt class="col-6">Active records</dt>
      <dd class="col-6 text-end">428</dd>

      <dt class="col-6">Publication</dt>
      <dd class="col-6 text-end">v17 · Current</dd>
    </dl>

    <!-- use the enclosing card as one semantic accessible link -->
  </div>
</div>
```

## 7.2 Data Hub card content

Cards may show:

- module name;
- icon;
- short description;
- active record count;
- authoritative active publication version plus secondary update state;
- last changed time;
- a whole-card, keyboard-accessible navigation link; do not add a separate
  `Open module →` link or nested controls.

Cards should **not** contain:

- Import buttons;
- Edit buttons;
- Delete buttons;
- lifecycle actions;
- row-level actions;
- complex filter controls.

Those belong inside the module.

The displayed publication version is always the active/distributed version.
Queued, building, or failed candidate versions are secondary state only and
must never be presented as current. Use readable states such as:

- `v17 · Current`;
- `v17 · Update scheduled`;
- `v17 · Building update`;
- `v17 · Update failed`;
- `Not published` when there is no active publication.

## 7.3 Data Hub icons

Icons should improve recognition, not decorate the page.

Guidelines:

- Use one coherent icon family.
- Use vendored/local assets only.
- Keep icon weight and size consistent.
- Pair every icon with text.
- Never make an icon the only accessible label.

Suggested conceptual mapping:

- Hydrants — hydrant / water-supply symbol.
- Personnel — people / crew symbol.
- Fire Plans — building / document-plan symbol.
- KLGV Plans — garden / allotment / map-plan symbol.
- Future datasets — choose an icon directly tied to the operational concept.

For KLGV, the visual should communicate **Kleingartenverein / allotment-garden plans**, not a generic unknown-file icon.

## 7.4 Data Hub grid

Desktop recommendation:

- two or three cards per row depending on available width;
- cards in one row should have equal visual height where practical;
- avoid squeezing four or more operational cards into one row.

At narrow widths the grid may collapse naturally.

The Data Hub itself may be visually card-based even though ordinary management lists should not be boxed.

---

# 8. Status language

Use Bootstrap semantic colors consistently:

```text
primary      ordinary primary action
success      healthy / completed / active
warning      pending / attention required
danger       failed / stale / lost / destructive
info         informational operational state
secondary    inactive / retired / neutral
```

Status must always include text.

Good:

```text
Active
Pending
Stale
Retired
Failed
Publication current
```

Bad:

```text
●
red dot
green badge with no label
```

Color communicates state, not decoration.

---

# 9. Overview and attention UI

Overview is attention-first.

Order:

```text
What requires attention?
↓
General operational state
↓
Where should I go to resolve it?
```

Do not turn Overview into a generic analytics dashboard.

Attention items should:

- represent real backend-supported conditions;
- explain why attention is required;
- include a clear destination;
- avoid duplicate counting of the same underlying scope/problem;
- not behave like a notification inbox.

Use concise operational wording:

```text
3 publication scopes require attention
2 Tablets are unassigned
1 administrator account requires action
```

Prefer a meaningful link to the affected resource over a generic “More”.

---

# 10. Management lists

All potentially large management lists must be:

- server-side bounded;
- deterministically ordered;
- server-filtered;
- paginated.

Standard structure:

```text
Existing resources

Search / filters

Showing 1–100 of 428

Results table

Previous                         Next
```

## 10.1 Table presentation

Baseline:

```html
<table class="table table-hover align-middle">
```

Management tables should normally be part of the page document flow.

Do not wrap desktop-first management lists in:

- `.table-responsive`;
- `overflow-auto`;
- `overflow-x-auto`;
- fixed-height containers;
- max-height containers;

unless horizontal overflow is genuinely required by the data.

The page owns vertical scrolling.

## 10.2 Columns

Avoid excessive columns.

Prefer:

```text
Primary record
Status
Operational context
Last changed
Actions
```

Secondary identifiers may appear below the primary name:

```text
HLF 1
Vehicle ID · 17
```

## 10.3 Record navigation

The primary record name or identifier links to detail.

```text
Primary name → detail
Actions ▼    → mutations
```

Do not make the whole row clickable.

Do not include a redundant `View details` item in Actions when the primary record already links to detail.

---

# 11. Filtering and search

Filtering happens server-side.

HTMX is preferred for fast result refresh.

Canonical Personnel pattern:

```html
<input
  type="search"
  hx-get="..."
  hx-trigger="input changed delay:1s"
  hx-target="#person-results"
  hx-swap="outerHTML"
  hx-include="#person-filter-form"
  hx-push-url="true">
```

Use approximately 1–2 seconds of debounce for free-text search.

The actual Personnel implementation applies this behavior to every `input` and
`select` in the normal GET filter form: search fields use `input changed
delay:1s`; selects use `change`; each request includes the whole form and
replaces only the results region. This makes typing, pasting, and clearing text
live without requiring Enter, while preserving ordinary GET form submission
when HTMX is unavailable. Criteria changes omit `page`, returning to page 1;
pagination links preserve active query/filter parameters and target that same
results region.

The debounce is driven by the text input event while the field remains focused.
Search must execute after the idle delay without requiring blur, clicking
elsewhere, Enter, or form submission.

Rules:

- filters should reflect meaningful visible columns/domain fields;
- preserve selected filters during pagination;
- typing, paste, and clearing must all refresh the server-side result set;
- preserve filters after relevant mutations where practical;
- Reset remains available;
- a separate Filter/Submit button is usually unnecessary when HTMX live filtering is used;
- do not load the entire dataset into JavaScript and filter client-side.

Historical/inactive records should normally remain hidden until explicitly selected.

---

# 12. Actions

## Module-level actions

The Actions section contains workflows that create or initiate module-level work.

Examples:

- Create Station
- Register Tablet
- Import Hydrants
- Import Personnel
- Create Administrator

Use a small number of clearly prioritized buttons.

## Resource actions

Actions affecting one existing resource belong:

- in the row `Actions ▼` dropdown; or
- on the detail page.

Examples:

- Edit Data
- Assign
- Mark inactive
- Retire
- Suspend
- Revoke
- Replace installation

Lifecycle transitions must come from backend rules.

Never use a generic free-form status dropdown to bypass domain lifecycle semantics.

## Destructive actions

Use `btn-danger` only for genuinely destructive actions.

Permanent Delete should normally be:

- on the detail page;
- explicitly confirmed;
- clearly separated from normal lifecycle actions.

---

# 13. Forms

All forms use Bootstrap’s form language.

Use:

```text
.form-label
.form-control
.form-select
.form-check
.form-text
.invalid-feedback
```

Example:

```html
<div class="mb-3">
  <label for="id_name" class="form-label">Name</label>
  <input
    id="id_name"
    name="name"
    class="form-control"
    type="text">
  <div class="form-text">Short permanent help text where useful.</div>
</div>
```

Rules:

- visible labels;
- one logical input per line unless closely related;
- avoid raw `{{ form.as_p }}` as final UI;
- use selects for bounded choices;
- use textarea only when genuinely multiline content is expected;
- validation errors appear adjacent to their field;
- helper text is permanent where the concept is non-obvious.

---

# 14. Modals

Use Bootstrap/HTMX modals for short focused interactions:

- create;
- edit;
- simple assignment;
- confirmation;
- short lifecycle actions.

Use dedicated pages for:

- batch imports;
- multi-step review;
- adoption;
- installation replacement;
- complex recovery;
- settings;
- large workflows.

## Modal behavior

Required flow:

```text
Click
→ HTMX GET
→ content inserted
→ Bootstrap modal opens
→ POST
→ invalid response remains in modal
→ valid response redirects or refreshes appropriate region
```

Do not trigger Bootstrap to open a modal before HTMX has inserted valid modal structure.

Use explicit button types:

```html
<button type="submit">Save</button>
<button type="button" data-bs-dismiss="modal">Cancel</button>
```

Validation errors must preserve entered values.

---

# 15. Detail pages

Detail pages are the authoritative place for:

- full identity;
- status;
- lifecycle/security context;
- audit/context;
- complex resource actions;
- permanent deletion where allowed.

Suggested layout:

```text
Breadcrumb
Resource name
Short identifier/context

Status & Context

Actions

Details

History / related resources

Danger zone
```

Do not crowd detail pages with every possible related record. Keep histories bounded.

---

# 16. Import pages

Imports are domain-specific and each scope owns an explicit user-facing
template/page while sharing the ingestion wizard services and partials.

A Hydrant import page imports Hydrants.
A Personnel import page imports Personnel.
A Station and Vehicle import page imports the paired Station/Vehicle CSV.

Personnel CSV uses the explicit `home_station` reference, never a generic
`station` column. It accepts an active Department Station Short Code or exact
full Station name; ambiguous references require review and missing references
never create Stations.

Do not use a cross-domain selector.

Import pages should contain:

```text
Back to [module]

Page title
Accepted formats
Download template
Import mode + help
Upload
Preview/review
Recent batches for this domain
```

Import mode belongs in the form, not in CSV rows. Do not render a select when
there is only one supported format or mode. The staging action is labelled
`Import and review`, and recent imports are domain-scoped, bounded, timestamped,
and newest first. Use explicit human-readable page titles; do not expose enum
identifiers. Hydrant import offers CSV and GeoJSON only.

Explain modes in plain language.

Example:

```text
Upsert
Create new records and update matching existing records.

Merge
Update provided values while retaining approved existing values where the
incoming field is intentionally blank.
```

Complex import review remains a dedicated page, not a modal. Its initial render
and HTMX replacements use the same shared review region. Keep Accept and Skip
at the top; both record staged decisions and advance to the next unresolved
item. The final item renders the review summary. Invalid corrective input stays
on the current item with its bound submitted values and field errors visible.
Canonical records change only through explicit final Apply.

For long-running synchronous administrator actions, give immediate, truthful
processing feedback. The submit control must disable while the request is in
flight to prevent duplicate submits, show a spinner and operation-specific
wording, and expose an accessible live status. Final import Apply uses
`Applying changes…` with `Processing this import. Large datasets may take a
minute.` Do not call synchronous processing a background job or invent progress
percentages; normal page flow remains preferred.

---

# 17. Settings pages

Settings pages use separate full-width cards.

Never place unrelated settings cards side by side.

Pattern:

```text
Tablet authorization
[settings...]
[Apply]

Personnel retention
[settings...]
[Apply]

Tablet asset numbering
[settings...]
[Apply]

Locale and time display
[settings...]
[Apply]
```

Each card:

- has a clear title;
- explains what the setting changes;
- contains only one coherent settings group;
- has its own Apply action;
- explains destructive or operational consequences.

Do not expose backend implementation details as user-facing settings unless they represent an authoritative supported policy.

---

# 18. Navigation and context

Navigation is role-specific.

Do not make one giant sidebar with disabled entries.

## Department Administrator

```text
Overview

Distributed Data
    Data Hub
    Publications

Infrastructure
    Stations
    Tablets

Administration
    Administrator Accounts
    System Settings
    Audit Logs
```

## Station Administrator

```text
Overview

Station
    Personnel
    Tablets
```

## System Administrator

```text
Overview
Departments

System Administration
    API Compatibility
    System Settings
    Audit / System Events
```

Data Hub modules do not each need permanent sidebar entries.

Pages not directly reachable from sidebar/Data Hub should provide a visible link back to their base scope.

---

# 19. Dates and time

Prefer readable operational display:

```text
4 minutes ago
Today · 14:42
14 Aug 2026 · 14:42
```

For expiry/security workflows prefer explicit time:

```text
Expires at 14:47
```

Locale/timezone may affect UI presentation.

Do not change signed/protocol/security timestamp semantics merely for display.

Department locale/time policy is presentation-only: use bounded IANA timezone
choices and render Department-scoped administrator pages at the presentation
boundary. Station Administrators inherit their Department policy. Global System
Administrator views use a stable system presentation and never inherit an
arbitrary tenant policy.

---

# 20. Accessibility

Every interactive element must work with:

- keyboard;
- mouse;
- touch.

Required:

- visible labels;
- semantic headings;
- semantic table headers;
- accessible modal/page titles;
- meaningful button text;
- visible focus states;
- understandable validation;
- status text in addition to color;
- no critical icon-only controls.

Prefer:

```text
Actions ▼
```

over unlabeled three-dot menus.

---

# 21. HTMX usage

HTMX is appropriate for:

- search/filter result refresh;
- pagination regions;
- modal loading/submission;
- read-only status refresh;
- Tablet API activity;
- workflow-state fragments;
- selective content refresh.

Do not replace the whole application shell unnecessarily.

HTMX is transport/progressive enhancement—not a client-side domain state store.

---

# 22. Alpine.js and JavaScript

Use Alpine.js only for small local visual state:

- sidebar collapse;
- disclosure controls;
- simple toggles.

Do not encode FireDash business rules in Alpine or custom JavaScript.

JavaScript should never determine authorization, lifecycle validity or publication semantics.

---

# 23. New Data Hub module checklist

Before adding a future Data Hub dataset/module, answer:

### Identity
- What is the module’s short human-readable name?
- What icon represents it clearly?
- What is the primary record name/identifier?

### Card
- One-line description?
- Active/current count?
- Publication state?
- Last changed?
- `Open module →` destination?

### Module page
- Page title and short description?
- Meaningful status/context?
- Create/import actions?
- Search/filter fields?
- Default active/current lifecycle?
- Detail link?
- Actions dropdown?
- Server-side pagination?

### Data lifecycle
- What is active?
- What is historical/inactive?
- What is published?
- What mutation dirties/rebuilds publication?
- Can records be permanently deleted?
- Is deletion different from retirement/deactivation?

### Import
- Accepted formats?
- Template?
- Upsert/Merge semantics?
- Preview/review?
- Ambiguous references fail safely?

### Security
- Department or station scope?
- Department Admin permissions?
- Station Admin permissions?
- System Admin visibility?
- Audit events?
- Reauthentication needed?

A new Data Hub module should not be considered complete until these questions have explicit answers.

---

# 24. UI anti-patterns

Avoid:

- a card around every table;
- nested scrolling management lists;
- hidden inline edit forms;
- giant dashboard grids;
- row-click navigation;
- redundant `View details` actions;
- free-form lifecycle status selects;
- critical icon-only buttons;
- unbounded tables;
- client-side filtering of entire datasets;
- cross-domain import selectors;
- destructive actions mixed with ordinary actions;
- decorative status colors without text;
- raw JSON/configuration dumps as the final admin UI;
- side-by-side full-width settings cards;
- fake UI for backend features that do not exist.

---

# 25. Definition of a FireDash-consistent addition

A new page or module is FireDash-consistent when:

- it fits the shared shell;
- the page title/context is immediately clear;
- actions are placed at the correct module/resource level;
- forms use Bootstrap consistently;
- short workflows use the shared modal pattern;
- complex workflows use dedicated pages;
- lists are bounded, filtered and paginated server-side;
- record names link to detail;
- lifecycle actions come from backend rules;
- status uses shared semantic language;
- Data Hub remains a gateway;
- authorization is enforced server-side;
- destructive/security-sensitive operations remain audited;
- the page remains understandable without product-specific UI training.
