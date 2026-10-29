> Historical implementation guide: capacity review requirements and examples in
> this guide are superseded by the current [file-library contract](file-libraries.md#device-storage-is-managed-locally).
> Tablet capacity telemetry, gates, review routes, warnings and approval controls
> are disabled; source-library quotas remain active.

# FireDash Phase 3 — File Management & File Explorer UI implementation guide

**Status:** Implemented shared File Explorer for Department and Station Administrators; retained as the implementation and acceptance guide.<br>
**Original planning baseline:** `dermetti/FireDash-Server`, Phase 2 commit `5d9f44436815cb6500a359eebbeeaf2a38318ef9`.<br>
**Audience:** Django/Bootstrap/HTMX coding agent. Follow `docs/FireDash_UI_Styleguide.md` as the overarching style system.  
**Goal:** One familiar, operationally safe PDF explorer UI used by **both Department and Station Administrators**, with **File Management as a parent page** and **File Explorer as a dedicated child page**. Reuse views/templates where practical while preserving fundamentally different authorization and publication workflows.

> **Example-status convention.** The shared explorer and routes described here are implemented. HTML examples remain illustrative patterns; exact context names and fragment structure may differ from the current templates. The implementation in the working tree is authoritative.

## 1. User experience contract

### 1.1 Overall architecture

```text
Sidebar: File Management  ───► GET /files/  (existing dispatcher)
                                   │
                                   └──► GET /files/<library-uuid>/  (existing `file-library`)
                                            File Management PARENT PAGE
                                            ├─ Storage usage and active access context
                                            ├─ Dept-wide capacity approvals (department admin)
                                            ├─ Own capacity-review requests (station admin)
                                            ├─ Temporary station access (department admin)
                                            ├─ Compact station publication (station admin only)
                                            └─ Primary "Open File Explorer" CTA
                                                        │
                                                        ▼
                                 GET /files/<library-uuid>/explorer/  (`file-library-explorer`)
                                            FILE EXPLORER SUBPAGE
                                            ├─ Back to File Management
                                            ├─ Breadcrumb navigation
                                            ├─ Upload PDFs / New folder
                                            ├─ Search + sorting
                                            ├─ Paginated file/folder table
                                            └─ Contextual action dialogs
```

Keep `file-library-index`, `file-library`, existing file PDF preview/download endpoints and sidebar navigation semantics intact. The parent page is **not** a second file table. The child page is **not** a second policy dashboard.

For department admins, **Publications** is the sole publication-control UI for department files. For station admins, the File Management parent retains a compact station publication control because they cannot access Publications. Temporary station access exposes neither kind of publication control.

### 1.1.1 One explorer experience, three authorized contexts (REQUIRED)

**The explorer is not a department-admin-only feature.** All three authorized contexts below must use the **same** `file-library-explorer` route, the same `explorer.html`, shared file rows, search/sort/pagination and action modal components; available actions and parent-page controls differ based on **server-side validated library scope and access mode**, not JavaScript or a client-supplied role value.

```text
Department Administrator ─► Department File Management parent
                        └─► Department File Explorer (normal management)
                        └─► Publications > Department files (publication control)
                        └─► Temporary station access activation (explicit confirmation)
                                  └─► Station File Management parent (temporary)
                                        └─► SAME File Explorer (restricted)

Station Administrator ────► Own Station File Management parent
                        ├─► SAME File Explorer (normal station management)
                        └─► Station publication control (on parent; no Publications access)
```

The shared explorer must be discoverable from **both** department and station File Management entry points, including the existing station sidebar and station Data Hub link; preserve the normal `/files/` dispatcher. Do **not** create a department-only explorer and a second bespoke station file table. Do **not** substitute the temporary-grant workflow for normal station-admin authority.

**Important edge case:** a person with department authority and some station membership does not automatically gain normal access to station libraries. Trust `services.require_library_access()` / `read_explorer().access_mode` and existing authorization invariants. Never infer `normal` from whether the URL contains a station UUID or the user merely has a station membership.

### 1.2 Design references, adapted narrowly

The familiar patterns are demonstrated in the official [Nextcloud Files manual](https://docs.nextcloud.com/server/stable/user_manual/en/files/access_webgui.html): folder breadcrumbs, file-name navigation, creation/upload control, per-row three-dot action menu and preview. **Do not implement** Nextcloud sharing, favorites, versions, comments, multi-select, trash, drag-and-drop or editor features: those are out of scope.

Use the [WAI-ARIA combobox pattern](https://www.w3.org/WAI/ARIA/apg/patterns/combobox/) for the station selector; a search input plus a second unrelated dropdown is **not** a combobox. Use the application's locally vendored Bootstrap 5 and HTMX; no CDN or new UI framework.

### 1.3 Precise visual hierarchy

**Parent page, desktop:**

```text
File Management
Department files · Example Department

[ Storage: 1.4 GB of 5 GB ]                         [ Open File Explorer → ]

Capacity reviews requiring approval  (only when pending, department admin)
  2 requests waiting                 [ Review request → ]

Temporary station access            (department admin ONLY)
  Access a station library temporarily to view, download or delete.
  [ Choose station… ]                 ← opens ONE searchable dialog

No publication editor, no file table, no upload or folder forms.
```

**Normal station-admin parent, desktop — SAME page layout with station-specific actions:**

```text
File Management
Station files · Station North

[ Storage: 340 MB of 2 GB ]                         [ Open File Explorer → ]

Your capacity reviews             (requester-owned, only when relevant)
  Operational map.pdf · Pending                       [ View review → ]

Station file publication
  v3 · Unpublished changes                            [ Publish Now ]

NO department-wide approval queue; NO temporary-access picker; NO Publications link.
```

**Temporary department-admin station parent — SAME page layout, restricted:**

```text
File Management
Temporary station access · Station North

[ Temporary access: View / Download / ONE Delete · Expires 16:30 ] [ Release ]

[ Storage: 340 MB of 2 GB ]                         [ Open File Explorer → ]

NO station publication, no upload forms, no approvals, no station-switch picker.
```

**Explorer child page, desktop:**

```text
← File Management
File Explorer                        Department files · Example Department

Files  ›  Operational plans  ›  2026

[ Upload PDFs ]   [ New folder ]                 [  Search files and folders      🔍  ]

  Name                                        Size       Modified        Actions
  📁 Deployment plans                        —          Oct 18          ⋯
  📄 Operational map.pdf                      1.8 MB     Oct 16          ⋯
  📄 Station overview.pdf                     4.2 MB     Oct 15          ⋯

                           Showing 3 items   [‹ Previous]  Page 1  [Next ›]
```

Mobile: toolbar wraps; names remain readable; size/modified may hide in narrow view but **accessible row actions must remain**. A desktop-wide table inside the existing app shell is appropriate; don't introduce an extra app sidebar or two independently scrolling panes.

**Temporary station-access explorer:** top warning `Temporary access · Station X · Expires ... · Release access`; only Open/Preview/Download/Delete in row menus; **no upload, new folder, rename, move, replace or publish**.

## 2. Existing code and authoritative boundaries

Read these files before editing:

| File | Purpose | Keep/use |
|---|---|---|
| `apps/file_library/explorer.py` | Phase 1 `read_explorer()`, typed read model, bounded page, search, folder ancestry and operation hints | Use for child page; do not rewrite the query model in the view |
| `apps/file_library/services.py` | Auth, temp access, mutations and folder constraints | Services are final authorization boundary |
| `apps/file_library/views.py` | Existing `index`, `library_view`, `select_station_access`, `document`, capacity review actions | Refactor carefully and preserve security |
| `apps/file_library/urls.py` | Existing `file-library`, `file-library-index`, PDF and access URLs | Keep registered explorer and action routes |
| `templates/file_library/library.html` | Shared File Management parent template | Keep file browsing in the shared child and partials |
| `templates/file_library/index.html` | Multi-library selector | Retain semantics |
| `apps/file_library/forms.py` | Validated station ModelChoice + confirm | Reuse its authorized queryset at POST time |
| `apps/file_library/publication.py` | `publish_now`, `publication_state` | Preserve for station admin only on parent |
| `apps/publications/views.py` | Department `department_files_publish` POST route prepared in Phase 1 | Used by Publications UI, not file explorer |
| `templates/publications/scope_detail.html` | Existing department scope actions/history | Hosts the authorized department-file publish action |
| `templates/components/_htmx_action_modal.html` | Existing action modal controller | Reuse or minimally improve, no parallel modal frameworks |
| `templates/components/_page_header.html` | Shared page heading | Use for parent and child |
| `templates/components/_alerts.html` | Shared flash feedback | Retain per-file outcomes and action errors |

**Existing route names, already implemented:** `file-library`, `file-library-index`, `file-library-document`, `file-library-station-access-select`, `file-library-station-access-release`, `file-library-capacity-review`, `portal-department-settings`, `publications-list`, `publications-scope-detail`, `publications-department-files-publish`.

**Implemented Phase 3 route names:** `file-library-explorer`, `file-library-explorer-action`, `file-library-station-access-choose` and `file-library-station-search`. The action route accepts a constrained action slug and an optional target UUID in the request, and calls existing service functions—not generic model edits.

### 2.1 Access and operation matrix

| Operation | Department own library | Station admin own station | Dept admin temporary station |
|---|---:|---:|---:|
| View folder / search | Yes | Yes | Yes |
| Preview/download | Yes | Yes | Yes |
| Upload / create folder | Yes | Yes | **No** |
| Rename / move / replace | Yes | Yes | **No** |
| Delete | Yes | Yes | Yes, **one successful deletion consumes access** |
| File publication | In **Publications UI** | Compact on parent | **No** |
| Temporary access selection | On department parent | **No** | Already active: show scope/expiry/release |
| Department capacity review decisions | Department parent and existing detail | Request/retry only, not approve | No override while temporary access active |

`ExplorerReadModel.operations` is a **display hint**. It is not authorization. The existing service layer must recheck persisted grants and role at POST time. If an expired grant is revoked during validation, preserve that revocation even when the intended action is denied.

### 2.2 Server-derived view contract per role

| Context | Parent File Management page | Shared Explorer subpage | Publication location | Capacity review visibility |
|---|---|---|---|---|
| **Department admin, own department library** (`library.department_id`, `ACCESS_NORMAL`) | Usage + Open Explorer + actionable department-wide pending approvals + temporary station picker | Full file/folder management, PDF preview/download | **Department Publications** only | All currently pending reviews in that department (including those requested by station admins), linked to the existing decision route; preserve other authorized personal review states where appropriate |
| **Station admin, own station library** (`library.station_id`, `ACCESS_NORMAL`) | Usage + Open Explorer + compact station publication panel + own capacity-review requests | Full file/folder management within *that station only* | **Station File Management parent** only | Own pending/approved/failed review actions and retry links; **no** department-wide approval queue or approve/reject buttons |
| **Department admin, temporarily authorized station library** (`library.station_id`, `ACCESS_TEMPORARY`) | Restricted-access banner + expiry + Release access + Open Explorer | Browse folders, preview/download and one successful deletion only | **None** | No approval/override affordances while the temporary grant is active; only existing separately authorized read context as needed |
| **System admin or unauthorized/cross-tenant actor** | Denied by existing scope checks | Denied on GET, modal GET/POST and PDF endpoints | None | None |

Derive parent flags on the server from the **actual validated access mode and scope** (example names: `is_department_library_admin`, `is_station_library_admin`, `is_temporary_station_access`, `can_publish_station_files`, `can_choose_temporary_station`, `can_review_department_capacity`). Do not trust role booleans in query strings or POST data; do not rely on a hidden DOM element to secure a mutation. Exact flags are proposed UI context and must be checked against existing service-level capabilities.

Do not inadvertently drop station-admin capacity-review visibility: the current `library_view` exposes requester-specific reviews, and `capacity_review` allows the authorized requester to retry an approved operation. Keep an equivalent station-parent entry point after removing the explorer table from that parent.

### 2.3 Route and direct-link contract

1. `GET /files/` chooses an authorized library as before. Department admins enter their **department** parent; station-only admins enter their **own station** parent; users with multiple authorized libraries retain a scoped chooser.
2. `GET /files/<library UUID>/` is the **same File Management parent view** for both normal roles and temporary mode, with different server-rendered sections.
3. `GET /files/<library UUID>/explorer/` is the **same File Explorer view** in all permitted modes, using `read_explorer()` to check authorization every time.
4. Every explorer modal GET, mutation POST, PDF preview and PDF download checks persisted library+entry scope independently. Foreign URLs must never reveal filenames or metadata.
5. Breadcrumb/back links return to the **matching** parent library. After temporary grant expiry/consumption, redirect to the user's authorized `/files/` destination; do not redirect to a station view that is now forbidden.
6. Preserve the existing `/stations/<station UUID>/data/` Station Data Hub link to File Management. Station admins must not need to visit the Department Data Hub or Department Publications to reach their own explorer.

## 3. File Management parent page — HTML pattern

Suggested template: keep `templates/file_library/library.html` as the **parent**. Suggested context variables: `library`, `access_mode`, `temporary_access`, `capacity_reviews`, `pending_review_page`, `publication` (only when station admin), `file_count`, `quota_bytes`, `usage_bytes`.

Example complete **parent skeleton** (illustrative context conditions; implement role checks in the view):

```django
{% extends "base.html" %}
{% block title %}File Management{% endblock %}
{% block content %}
  {% include "components/_page_header.html" with title="File Management" description="Manage the files in this library." %}

  <p class="text-body-secondary mb-2">
    {% if library.department_id %}
      Department files · {{ library.department.name }}
    {% else %}
      Station files · {{ library.station.name }}
    {% endif %}
  </p>

  {% if access_mode == "temporary" %}
    <div class="alert alert-warning d-flex flex-wrap align-items-center justify-content-between gap-2" role="status">
      <div>
        <strong>Temporary station access · {{ library.station.name }}</strong>
        <span class="d-block small">View, download or delete only. Expires {{ temporary_access.expires_at }}.</span>
      </div>
      <form method="post" action="{% url 'file-library-station-access-release' %}">
        {% csrf_token %}
        <button type="submit" class="btn btn-outline-dark btn-sm">Release access</button>
      </form>
    </div>
  {% endif %}

  {% if library.usage_bytes > library.quota_bytes %}
    <div class="alert alert-danger" role="alert">
      This library is over quota. Storage-increasing operations are blocked.
    </div>
  {% endif %}

  <section class="d-flex flex-wrap justify-content-between align-items-center gap-3 mb-4"
           aria-label="Library overview">
    <div>
      <div class="fw-semibold">{{ library.name }}</div>
      <div class="text-body-secondary small">
        {{ file_count }} PDF{{ file_count|pluralize }} ·
        {{ library.usage_bytes|filesizeformat }} of {{ library.quota_bytes|filesizeformat }} used
      </div>
    </div>
    <a class="btn btn-primary" href="{% url 'file-library-explorer' library.id %}">
      Open File Explorer <span aria-hidden="true">→</span>
    </a>
  </section>

  {% if can_review_department_capacity and pending_review_page.paginator.count %}
    <section aria-labelledby="file-capacity-heading" class="mb-4">
      <h2 id="file-capacity-heading" class="h5">Capacity reviews requiring approval</h2>
      {# Preserve existing scoped, paginated request links. Do not hide them. #}
      {% include "file_library/_pending_capacity_reviews.html" %}
    </section>
  {% endif %}

  {% if can_choose_temporary_station %}
    <section class="mb-4" aria-labelledby="station-access-heading">
      <h2 id="station-access-heading" class="h5">Temporary station access</h2>
      <p class="text-body-secondary mb-2">
        Temporarily view, download or delete files in an authorized station library.
      </p>
      <button type="button" class="btn btn-outline-secondary"
              data-bs-toggle="modal" data-bs-target="#station-access-modal">
        Choose station…
      </button>
      {% include "file_library/_station_access_modal.html" %}
    </section>
  {% endif %}

  {% if can_publish_station_files %}
    <section aria-labelledby="station-publish-heading" class="border-top pt-3 mt-3">
      <h2 id="station-publish-heading" class="h5">Station file publication</h2>
      <p class="text-body-secondary small">
        {{ publication.label }}
        {% if publication.current %} · Active version {{ publication.current.version_number }}{% endif %}
      </p>
      <form method="post" action="{% url 'file-library' library.id %}">
        {% csrf_token %}
        <input type="hidden" name="action" value="publish">
        <button type="submit" class="btn btn-outline-primary btn-sm">Publish Now</button>
      </form>
      <p class="form-text">Publication does not confirm delivery to any tablet.</p>
    </section>
  {% endif %}
{% endblock %}
```

**Critical:** `can_choose_temporary_station`, `can_publish_station_files`, and `can_review_department_capacity` are **proposed server-derived booleans**, not existing permissions. Assign them only after authoritative role validation. A department admin's temporary station library must never present normal station publish controls.

Keep parent lightweight: NO folder listings, source-file forms, upload button, nested file action accordions, or second File Explorer grid. The file-management index must still work for multiple authorized libraries.

### 3.1 Required role variations of the SAME parent template

**Normal Department Administrator** — the above parent skeleton shows their library details, department-wide pending review queue and the temporary-access picker. `can_publish_station_files=False`; do not display a department-file publication panel or Publish Now button. Link them to the existing Publications section for lifecycle work, not to a duplicate file-library publication form.

**Normal Station Administrator** — render that **same** `library.html` parent with station name, usage and **Open File Explorer**. Do **not** show `can_choose_temporary_station` or department-wide capacity approvals. The parent must retain both a compact station-only publish panel and a section for **the station administrator's own capacity warning/review requests** (including status and existing retry link when available), rather than losing the workflow when the existing file table is extracted. Example partial (names/context are proposed):

```django
{% if is_station_library_admin %}
  <section class="mb-4" aria-labelledby="station-reviews-heading">
    <h2 id="station-reviews-heading" class="h5">Your capacity reviews</h2>
    {% for review in station_requester_reviews %}
      <div class="d-flex flex-wrap justify-content-between gap-2 border-bottom py-2">
        <div>
          <strong>{{ review.operation.name }}</strong>
          <span class="text-body-secondary small">· {{ review.decision }}</span>
        </div>
        <a href="{% url 'file-library-capacity-review' review.id %}">View review</a>
      </div>
    {% empty %}
      <p class="text-body-secondary small">No outstanding capacity reviews.</p>
    {% endfor %}
  </section>
  {% if can_publish_station_files %}
    {# Use the station-only publication panel from the parent skeleton. #}
  {% endif %}
{% endif %}
```

Derive `station_requester_reviews` from the actor and authorized station library using existing review semantics; do not show other requesters' private requests unless the established authorization explicitly permits that read. Approved review retry remains in the existing `capacity_review` page, with all existing operation and fingerprint requirements.

**Department Administrator with temporary station grant** — render that **same** `library.html` parent with a prominent **Temporary station access** banner, station name, expiry/release button and **Open File Explorer**. Do **not** treat the visitor as a normal station admin. Render neither the station publish panel, the department picker nor the department-wide approval queue on this temporary station parent. All file mutation affordances except deletion disappear, but source authorization is still enforced server-side.

Suggested flags for template context (illustrative, not trusted without backend checks):

```python
# Only after loading the scoped Library and checking services.require_library_access(...)
is_department_library_admin = bool(library.department_id and access == services.ACCESS_NORMAL)
is_station_library_admin = bool(library.station_id and access == services.ACCESS_NORMAL)
is_temporary_station_access = bool(library.station_id and access == services.ACCESS_TEMPORARY)
can_publish_station_files = is_station_library_admin
can_choose_temporary_station = is_department_library_admin
can_review_department_capacity = is_department_library_admin
```

These examples do **not** replace real service authorization or account for every app-specific role nuance; service checks remain authoritative. The station-only publish form must be accepted on POST only for the normal authorized station context. The department file legacy POST publication path must be denied after moving it to Publications.

## 4. File Explorer subpage — HTML pattern

Suggested route: `GET /files/<uuid:library_id>/explorer/`, name `file-library-explorer`. Use `read_explorer(actor=request.user, library_id=..., folder_id=..., query=..., sort=..., descending=..., page=...)` and validate query args. Return 404 for unknown/non-library folder and 403 for unauthorized libraries (follow existing app denial patterns; don't leak foreign entry names).

### 4.1 Full-page skeleton

Suggested new file `templates/file_library/explorer.html`:

```django
{% extends "base.html" %}
{% block title %}File Explorer · {{ library.name }}{% endblock %}
{% block content %}
  <div class="mb-3">
    <a class="link-secondary text-decoration-none"
       href="{% url 'file-library' library.id %}">
      <span aria-hidden="true">←</span> File Management
    </a>
  </div>

  {% include "components/_page_header.html" with title="File Explorer" description="Browse and manage PDF files and folders." %}
  <p class="small text-body-secondary mb-3">
    {% if library.department_id %}Department files · {{ library.department.name }}
    {% else %}Station files · {{ library.station.name }}{% endif %}
  </p>

  {% if access_mode == "temporary" %}
    {% include "file_library/_temporary_access_banner.html" %}
  {% endif %}

  <nav aria-label="Folder path" class="mb-3">
    <ol class="breadcrumb mb-0">
      <li class="breadcrumb-item">
        <a href="{% url 'file-library-explorer' library.id %}">Files</a>
      </li>
      {% for folder in breadcrumbs %}
        <li class="breadcrumb-item{% if forloop.last %} active{% endif %}"
            {% if forloop.last %}aria-current="page"{% endif %}>
          {% if forloop.last %}
            {{ folder.name }}
          {% else %}
            <a href="{% url 'file-library-explorer' library.id %}?folder={{ folder.id }}">
              {{ folder.name }}
            </a>
          {% endif %}
        </li>
      {% endfor %}
    </ol>
  </nav>

  <div id="file-explorer-content">
    {% include "file_library/_explorer_listing.html" %}
  </div>

  {% include "components/_htmx_action_modal.html" with modal_container_id="file-explorer-action-modal" %}
{% endblock %}
```

Breadcrumb note: In the library root, `Files` is the only breadcrumb and should read as the current location (use `aria-current="page"` on root when `breadcrumbs` is empty; do not create an empty trailing crumb).

**Do not use a clickable whole table row.** Use semantic anchors for folders and previews, and a separate accessible menu button. Do not put form controls inside links.

### 4.1.1 One explorer template, not one per role

This **exact subpage and all its partials** must serve a normal department admin and a normal station admin: folder paths, PDF preview/download, search, sorting, pagination, upload, folder creation, rename, move, replace and delete must behave identically except for the scoped library. Use the same Bootstrap toolbar, file icons, row dropdown and modal design. A station admin should not see an inferior legacy form-based file interface.

In temporary mode, render the **same** explorer with an explicit restricted-access banner, disable or omit Upload PDFs/New folder at the toolbar, and omit Rename/Move/Replace in the row actions. Keep normal folder browsing, PDF preview/download and confirmed Delete. On successful delete, grant revocation must complete before redirect. The server must reject direct POSTs to hidden actions regardless of HTML.

For normal station admins, do not add a Publications navigation link or a Publish Now button to the explorer. The **station parent**, not the explorer, owns station publication controls. Likewise, the department explorer must not expose department publication mutations.

### 4.2 Toolbar + search + sortable table

Suggested partial `templates/file_library/_explorer_listing.html`:

```django
<section aria-label="File explorer">
  <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
    <div class="d-flex flex-wrap gap-2">
      {% if can_manage_files %}
        <button type="button" class="btn btn-primary"
                hx-get="{% url 'file-library-explorer-action' library.id 'upload' %}{% if current_folder %}?folder={{ current_folder.id }}{% endif %}"
                hx-target="#file-explorer-action-modal">
          Upload PDFs
        </button>
        <button type="button" class="btn btn-outline-secondary"
                hx-get="{% url 'file-library-explorer-action' library.id 'new-folder' %}{% if current_folder %}?folder={{ current_folder.id }}{% endif %}"
                hx-target="#file-explorer-action-modal">
          New folder
        </button>
      {% endif %}
    </div>

    <form method="get" action="{% url 'file-library-explorer' library.id %}"
          class="d-flex gap-2 flex-grow-1 justify-content-end" role="search"
          hx-get="{% url 'file-library-explorer' library.id %}"
          hx-target="#file-explorer-content" hx-select="#file-explorer-content > *"
          hx-swap="innerHTML" hx-push-url="true" hx-trigger="submit">
      {% if current_folder %}<input type="hidden" name="folder" value="{{ current_folder.id }}">{% endif %}
      <label class="visually-hidden" for="explorer-search">Search files and folders</label>
      <input type="search" id="explorer-search" name="q" class="form-control"
             placeholder="Search files and folders" value="{{ query }}" maxlength="255">
      <button class="btn btn-outline-secondary" type="submit">Search</button>
    </form>
  </div>

  {% if query %}
    <p class="small text-body-secondary mb-2">
      Search results for <strong>{{ query }}</strong> ·
      <a href="{% url 'file-library-explorer' library.id %}{% if current_folder %}?folder={{ current_folder.id }}{% endif %}">
        Clear search
      </a>
    </p>
  {% endif %}

  <div class="table-responsive">
    <table class="table table-hover align-middle">
      <thead>
        <tr>
          <th scope="col">Name</th>
          <th scope="col" class="text-nowrap">Size</th>
          <th scope="col" class="text-nowrap">Modified</th>
          <th scope="col" class="text-end">Actions</th>
        </tr>
      </thead>
      <tbody>
        {% for item in items %}
          {% include "file_library/_explorer_row.html" %}
        {% empty %}
          <tr><td colspan="4" class="text-center text-body-secondary py-5">
            {% if query %}
              No matching files or folders. Try another search.
            {% else %}
              This folder is empty.
              {% if can_manage_files %}Upload PDFs or create a folder to get started.{% endif %}
            {% endif %}
          </td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="d-flex flex-wrap justify-content-between align-items-center gap-2">
    <span class="small text-body-secondary">
      {{ page.paginator.count }} item{{ page.paginator.count|pluralize }}
    </span>
    {% include "file_library/_explorer_pagination.html" %}
  </div>
</section>
```

**Implementation notes:** `file-library-explorer-action` is implemented; management capabilities are server-derived from the access mode. `hx-select`/`hx-target` above are illustrative; ensure the returned HTMX fragment structure exactly matches the swap target and does not duplicate IDs. An alternative is to return only `_explorer_listing.html` for HX requests and use `hx-swap="outerHTML"` on `#file-explorer-content`. Choose one coherent strategy, test it, and retain non-JavaScript GET behavior.

**Sorting:** Add simple query-linked Name / Size / Modified headers or a `Sort by` select. Supported values from `explorer.py` are `name`, `modified`, `size`, with `descending` boolean and fixed UUID tie breaker. Preserve `folder`, `q`, sort and direction in pagination/search links. Reset to page 1 on new search/sort. Do not build all entries in Python to sort them.

**Query UX:** Existing `read_explorer` searches the whole authorized library even when a folder is selected. Show contextual ancestor paths on search results so users know where files are. Make this scope clear in search help text. Do not imply current-folder-only search.

### 4.3 File and folder row, correctly separated actions

Suggested `templates/file_library/_explorer_row.html`:

```django
<tr id="entry-{{ item.entry.id }}">
  <td>
    <div class="d-flex align-items-start gap-2">
      <span aria-hidden="true" class="text-secondary flex-shrink-0">
        {% if item.entry.is_folder %}📁{% else %}📄{% endif %}
      </span>
      <div class="min-width-0">
        {% if item.entry.is_folder %}
          <a class="fw-semibold text-decoration-none"
             href="{% url 'file-library-explorer' library.id %}?folder={{ item.entry.id }}">
            {{ item.entry.name }}
          </a>
        {% else %}
          <a class="fw-semibold text-decoration-none"
             href="{% url 'file-library-document' library.id item.entry.id %}"
             target="_blank" rel="noopener">
            {{ item.entry.name }}
          </a>
        {% endif %}
        {% if query %}
          <small class="d-block text-body-secondary">
            In Files{% for ancestor in item.ancestors %} / {{ ancestor.name }}{% endfor %}
          </small>
        {% endif %}
      </div>
    </div>
  </td>
  <td class="text-nowrap">
    {% if item.entry.is_folder %}—{% else %}{{ item.entry.content.size_bytes|filesizeformat }}{% endif %}
  </td>
  <td class="text-nowrap">
    {{ item.entry.modified_at|date:"Y-m-d H:i" }}
  </td>
  <td class="text-end">
    <div class="dropdown">
      <button class="btn btn-sm btn-outline-secondary dropdown-toggle" type="button"
              data-bs-toggle="dropdown" aria-expanded="false"
              aria-label="Actions for {{ item.entry.name }}">
        Actions
      </button>
      <ul class="dropdown-menu dropdown-menu-end">
        {% if item.entry.is_folder %}
          <li><a class="dropdown-item"
                 href="{% url 'file-library-explorer' library.id %}?folder={{ item.entry.id }}">Open</a></li>
        {% else %}
          <li><a class="dropdown-item" target="_blank" rel="noopener"
                 href="{% url 'file-library-document' library.id item.entry.id %}">Preview</a></li>
          <li><a class="dropdown-item"
                 href="{% url 'file-library-document' library.id item.entry.id %}?download=1">Download</a></li>
        {% endif %}

        {% if can_manage_files %}
          <li><hr class="dropdown-divider"></li>
          <li><button class="dropdown-item" type="button"
                hx-get="{% url 'file-library-explorer-action' library.id 'rename' %}?entry={{ item.entry.id }}"
                hx-target="#file-explorer-action-modal">Rename</button></li>
          <li><button class="dropdown-item" type="button"
                hx-get="{% url 'file-library-explorer-action' library.id 'move' %}?entry={{ item.entry.id }}"
                hx-target="#file-explorer-action-modal">Move</button></li>
          {% if not item.entry.is_folder %}
            <li><button class="dropdown-item" type="button"
                  hx-get="{% url 'file-library-explorer-action' library.id 'replace' %}?entry={{ item.entry.id }}"
                  hx-target="#file-explorer-action-modal">Replace PDF</button></li>
          {% endif %}
        {% endif %}
        <li><hr class="dropdown-divider"></li>
        <li><button class="dropdown-item text-danger" type="button"
              hx-get="{% url 'file-library-explorer-action' library.id 'delete' %}?entry={{ item.entry.id }}"
              hx-target="#file-explorer-action-modal">Delete</button></li>
      </ul>
    </div>
  </td>
</tr>
```

**Production icon rule:** Emoji are *only placeholders in this illustrative example*. Implement small, distinct **local SVG** file/folder icons or reuse existing first-party icon assets; do not add emoji file icons to the finished FireDash table. Keep all visible names escaped by Django, including untrusted file names.

**Responsive dropdown issue:** `.table-responsive` applies overflow and can clip Bootstrap menus. Verify the opened action menu in the last row on desktop and mobile. Use an approved Bootstrap Popper strategy or move the menu outside the clipping container, rather than inventing `z-index: 999999`.

### 4.4 Pagination example

```django
<nav aria-label="File explorer pages">
  <ul class="pagination pagination-sm mb-0">
    <li class="page-item{% if not page.has_previous %} disabled{% endif %}">
      {% if page.has_previous %}
        <a class="page-link" href="?{{ previous_querystring }}">Previous</a>
      {% else %}<span class="page-link" aria-disabled="true">Previous</span>{% endif %}
    </li>
    <li class="page-item disabled">
      <span class="page-link" aria-current="page">Page {{ page.number }} of {{ page.paginator.num_pages }}</span>
    </li>
    <li class="page-item{% if not page.has_next %} disabled{% endif %}">
      {% if page.has_next %}
        <a class="page-link" href="?{{ next_querystring }}">Next</a>
      {% else %}<span class="page-link" aria-disabled="true">Next</span>{% endif %}
    </li>
  </ul>
</nav>
```

Compute `previous_querystring` and `next_querystring` on the server using a copied `QueryDict`, setting only `page`. Do not concatenate raw user input in HTML. Page size defaults to 25, hard upper bound 100 from `explorer.py`. Nonexistent page returns a friendly bounded UI or an explicit client error consistent with established tests; do not silently fetch an unbounded page.

## 5. Modal design and complete form patterns

Use one reusable dynamic modal container `file-explorer-action-modal` and the existing Bootstrap/HTMX modal behavior. All modal triggers must have a working fallback when JS is absent: route to an ordinary GET action form page, POST back to a validated route and redirect to the explorer after success. **Never put one `<form>` inside another**. Never render all action forms inside every row.

General Bootstrap modal structure:

```django
<div class="modal fade" id="file-explorer-form-modal" tabindex="-1"
     aria-labelledby="file-explorer-modal-title" aria-hidden="true">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header">
        <h2 id="file-explorer-modal-title" class="modal-title fs-5">{{ modal_title }}</h2>
        <button type="button" class="btn-close" data-bs-dismiss="modal"
                aria-label="Close dialog"></button>
      </div>
      <form method="post" action="{{ action_url }}" enctype="{{ enctype }}">
        {% csrf_token %}
        <div class="modal-body">
          {# One action-specific field group, and inline validation errors #}
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary"
                  data-bs-dismiss="modal">Cancel</button>
          <button type="submit" class="btn btn-primary">{{ submit_label }}</button>
        </div>
      </form>
    </div>
  </div>
</div>
```

**Do not literally render `enctype=""`**: add `enctype="multipart/form-data"` only to upload and replacement forms. For form errors, rerender the same modal content with `aria-invalid="true"` and input-associated error IDs. Reopening a failed form must not lose the filename field or silently submit a different entry. HTTP 422 is optional; if used, configure HTMX error swap handling explicitly.

### 5.1 Upload PDFs modal

```django
<form method="post" action="{{ action_url }}" enctype="multipart/form-data">
  {% csrf_token %}
  <input type="hidden" name="action" value="upload">
  {% if current_folder %}<input type="hidden" name="folder" value="{{ current_folder.id }}">{% endif %}
  <div class="modal-body">
    <p class="small text-body-secondary">Destination: {{ folder_display_path }}</p>
    <label for="upload-pdfs" class="form-label">Choose PDF files</label>
    <input id="upload-pdfs" name="pdfs" type="file" accept=".pdf,application/pdf"
           multiple required class="form-control">
    <p class="form-text">PDFs are validated and sanitized before being accepted.
      Maximum size per PDF: {{ max_file_bytes|filesizeformat }}.</p>
    <div role="status" aria-live="polite" id="upload-progress"></div>
  </div>
  <div class="modal-footer">
    <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
    <button type="submit" class="btn btn-primary">Upload PDFs</button>
  </div>
</form>
```

Validate `current_folder` on the server, not by trusting the hidden value. Preserve per-file success/error reporting and link to the existing capacity-review page. **A capacity warning is a request for review, not an upload success or tablet-delivery confirmation**. Keep standard file-picker as the baseline; drag-and-drop is out of scope.

### 5.2 New folder modal

```django
<form method="post" action="{{ action_url }}">
  {% csrf_token %}
  <input type="hidden" name="action" value="folder">
  <input type="hidden" name="folder" value="{{ parent_folder_id|default:'' }}">
  <div class="modal-body">
    <label class="form-label" for="new-folder-name">Folder name</label>
    <input id="new-folder-name" name="name" class="form-control" maxlength="255"
           required autofocus value="{{ submitted_name|default:'' }}">
    <div class="form-text">The folder will be created in {{ parent_display_path }}.</div>
  </div>
  <div class="modal-footer">
    <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
    <button type="submit" class="btn btn-primary">Create folder</button>
  </div>
</form>
```

### 5.3 Rename modal

```django
<form method="post" action="{{ action_url }}">
  {% csrf_token %}
  <input type="hidden" name="action" value="rename">
  <input type="hidden" name="entry" value="{{ entry.id }}">
  <div class="modal-body">
    <p class="small text-body-secondary">Renaming {{ entry.name }}</p>
    <label class="form-label" for="rename-entry">New name</label>
    <input id="rename-entry" class="form-control" name="name"
           value="{{ proposed_name|default:entry.name }}" maxlength="255" required>
  </div>
  <div class="modal-footer">
    <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
    <button type="submit" class="btn btn-primary">Save name</button>
  </div>
</form>
```

Do not allow removal of `.pdf` from document names, path traversal or duplicate names. The existing service is authoritative.

### 5.4 Move modal — destination picker, not one dropdown per row

```
Move “Operational map.pdf”

Destination
  Files                                  [Select]
  › Plans                               [Open]
  › Maps                                [Open]
  › Incident packs                      [Open]

[Cancel]                              [Move here]
```

Offer a server-side, **bounded** destination-folder browser inside the modal using existing `scoped_entry()` and ancestry constraints. A simple paginated set of direct child folders with `Open` links/buttons, `Up one level`, and `Select this location` is more predictable than an enormous select. Validate destination via the current `edit_entry(..., move=True, parent_id=...)` service. For ordinary POST fallback, show the same folder browser as a standalone page.

Do not fetch all folder paths in every file row. Do not permit a folder to be moved into itself or one of its descendants, another library, or beyond maximum depth. Root destination corresponds to `parent_id=None`.

### 5.5 Replace PDF modal

```django
<form method="post" action="{{ action_url }}" enctype="multipart/form-data">
  {% csrf_token %}
  <input type="hidden" name="action" value="replace">
  <input type="hidden" name="entry" value="{{ entry.id }}">
  <div class="modal-body">
    <p>Replace <strong>{{ entry.name }}</strong>.</p>
    <p class="small text-body-secondary">
      The logical document identity and displayed filename stay the same.
    </p>
    <label for="replacement-pdf" class="form-label">Replacement PDF</label>
    <input id="replacement-pdf" type="file" name="pdf" class="form-control"
           accept=".pdf,application/pdf" required>
  </div>
  <div class="modal-footer">
    <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
    <button type="submit" class="btn btn-primary">Replace PDF</button>
  </div>
</form>
```

Keep current sanitization/quota/capacity review behavior; don't bypass the existing `upload_pdf(..., replace_id=...)` implementation.

### 5.6 Delete confirmation modal

```django
<form method="post" action="{{ action_url }}">
  {% csrf_token %}
  <input type="hidden" name="action" value="delete">
  <input type="hidden" name="entry" value="{{ entry.id }}">
  <div class="modal-body">
    <p>
      Delete <strong>{{ entry.name }}</strong>?
      {% if entry.is_folder %}
        This will also delete all files and subfolders inside it.
      {% endif %}
    </p>
    <div class="form-check">
      <input class="form-check-input" type="checkbox" value="yes"
             id="delete-confirm" name="confirm" required>
      <label class="form-check-label" for="delete-confirm">
        I understand this deletion cannot be undone from the file explorer.
      </label>
    </div>
    {% if access_mode == "temporary" %}
      <p class="text-warning-emphasis small mt-2">
        One successful deletion immediately ends temporary station access.
      </p>
    {% endif %}
  </div>
  <div class="modal-footer">
    <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
    <button type="submit" class="btn btn-danger">Delete</button>
  </div>
</form>
```

No fake trash/recycle feature. After a successful *temporary* delete, redirect to an authorized File Management page instead of attempting to rerender the now-inaccessible station explorer. A failed delete must not consume the grant.

## 6. Temporary station access — SINGLE autocomplete dialog

**Location:** parent File Management page for a department admin. **Not** inside the Explorer toolbar. The activation destination is the selected station's *parent* File Management page, which then links to that station's explorer.

### 6.1 Visual design

```text
┌───────────────────────────────────────────────────────────┐
│ Temporary station access                              [×] │
├───────────────────────────────────────────────────────────┤
│ Choose an active station. Access expires in six hours.    │
│                                                           │
│ Station                                                   │
│ [🔍 Search station name or short code...                 ] │
│  ┌─────────────────────────────────────────────────────┐  │
│  │ Station Alpha                                  ST-A │  │
│  │ Station Alpine                                ST-AL │  │
│  │ Station South                                 ST-S  │  │
│  └─────────────────────────────────────────────────────┘  │
│                                                           │
│ Selected: Station Alpha (ST-A)                             │
│ [ ] I understand this temporary access ...                │
├───────────────────────────────────────────────────────────┤
│                                  [Cancel] [Activate access]│
└───────────────────────────────────────────────────────────┘
```

### 6.2 Modal skeleton (progressive enhancement)

```django
<div class="modal fade" id="station-access-modal" tabindex="-1"
     aria-labelledby="station-access-title" aria-hidden="true">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header">
        <h2 class="modal-title fs-5" id="station-access-title">Temporary station access</h2>
        <button class="btn-close" type="button" data-bs-dismiss="modal"
                aria-label="Close dialog"></button>
      </div>
      <form method="post" action="{% url 'file-library-station-access-select' %}"
            id="temporary-station-form">
        {% csrf_token %}
        <input type="hidden" name="department_id" value="{{ library.department_id }}">
        <input type="hidden" name="station_id" id="chosen-station-id" value="">
        <div class="modal-body">
          <p class="text-body-secondary small">
            Temporarily view, download or delete files in one active station library.
          </p>
          <label class="form-label" for="station-combobox">Station</label>
          <input id="station-combobox" type="search" class="form-control"
                 placeholder="Search station name or short code"
                 role="combobox" aria-autocomplete="list"
                 aria-expanded="false" aria-controls="station-listbox"
                 aria-describedby="station-selection-status"
                 autocomplete="off" name="q"
                 hx-get="{% url 'file-library-station-search' library.department_id %}"
                 hx-trigger="input changed delay:250ms"
                 hx-target="#station-search-results" hx-swap="innerHTML"
                 hx-include="this">
          <div id="station-search-results" class="mt-1">
            <ul id="station-listbox" role="listbox" class="list-group"
                aria-label="Matching stations"></ul>
          </div>
          <p id="station-selection-status" role="status" aria-live="polite"
             class="small text-body-secondary mt-2">No station selected.</p>
          <div class="form-check mt-3">
            <input id="station-access-confirm" type="checkbox" name="confirm" value="yes"
                   class="form-check-input" required>
            <label class="form-check-label" for="station-access-confirm">
              I understand access expires after six hours and allows one successful deletion.
            </label>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary"
                  data-bs-dismiss="modal">Cancel</button>
          <button type="submit" class="btn btn-primary" id="activate-station-access"
                  disabled>Activate access</button>
        </div>
      </form>
    </div>
  </div>
</div>
```

`file-library-station-search` is implemented as a read-only, department-authorized endpoint returning bounded active-station results. The POST `file-library-station-access-select` exists and must keep validating the selected UUID with `TemporaryStationAccessForm`. The modal can be AJAX-enhanced, but the choice must be final and checked server-side.

### 6.3 Returned search-result partial

Suggested `templates/file_library/_station_search_results.html`:

```django
<ul id="station-listbox" role="listbox" class="list-group shadow-sm"
    aria-label="Matching stations">
  {% for station in results %}
    <li id="station-option-{{ forloop.counter0 }}" role="option"
        aria-selected="false" tabindex="-1"
        data-station-id="{{ station.id }}"
        data-station-label="{{ station.name }} ({{ station.short_code }})"
        class="list-group-item list-group-item-action d-flex justify-content-between">
      <span>{{ station.name }}</span>
      <span class="small text-body-secondary">{{ station.short_code }}</span>
    </li>
  {% empty %}
    <li class="list-group-item text-body-secondary" role="presentation">
      No matching active stations.
    </li>
  {% endfor %}
</ul>
```

Bound to **10–20** results per request, deterministic ordering by name and UUID, scoped to the requesting department, active stations only. Use escape-by-default Django. Include server-side length cap on query text and avoid unbounded table scans when possible.

### 6.4 Required combobox controller behavior — do not skip

The HTML attributes alone do **not** make a working combobox. Implement a small, dedicated JS controller (local static file or a component-specific script), and manually test keyboard/pointer interactions outside pytest:

1. Input typing immediately **clears** any previous `station_id`, selected-label display and confirmation checkbox; disable activation until a fresh choice is made. This prevents choosing A, typing B, then accidentally submitting A.
2. `ArrowDown` / `ArrowUp`: cycle or move among the currently rendered selectable `<li role="option">` items; update `aria-activedescendant`, visual active row and `aria-selected` without moving DOM focus off the search input.
3. `Enter` selects the active station without accidentally submitting the form; copy `data-station-id` to the hidden input and show the label in the live selected-station status.
4. Pointer click selects a listbox option, retains the UUID and closes suggestions.
5. `Escape` closes suggestions without submitting or clearing a previously accepted selection unless deliberately specified. `Tab` exits the widget predictably.
6. On HTMX `afterSwap`, reset the highlight index, clear dangling `aria-activedescendant`, and update `aria-expanded` based on option presence. Ignore stale/out-of-order result responses if they no longer match the current input; do not replace a later search with earlier results.
7. Keep `aria-controls` pointing to an existing `id="station-listbox"` even when empty. Every `aria-activedescendant` must refer to an existing option ID. Use `role="status"` announcements for selection and empty results.
8. Disable activation when the hidden station ID is empty; server validation must **still** reject empty, inactive or cross-department IDs, and missing confirmation.
9. Show an initial bounded set of options when the dialog opens, or a clear instruction to type. Never preload 100+ stations in the HTML.
10. Test with at least 120 stations, including similarly named stations, stale selections, no matches, foreign stations, expired grants, mouse and keyboard paths.

**Important:** Do not put `<button>` elements *inside* elements with `role="option"` for this pattern. The controller manages option selection on the listbox. Conversely, do not render a visually clickable `<li>` without implementing pointer and keyboard behavior.

### 6.5 No-JavaScript fallback

A disabled-JS browser must still be able to activate authorized temporary access. **Do not rely solely on the JS-driven hidden input.** Provide an ordinary GET search form and server-rendered `<select name="station_id">` on an accessible fallback page or `<noscript>` block. E.g.:

```django
<noscript>
  <p class="small">JavaScript is not enabled. Use the standard station search form.</p>
  <a href="{% url 'file-library' library.id %}?station_search=1">
    Open standard station selection
  </a>
</noscript>
```

The standard selection view should accept `station_q` through GET, return at most 25 options, show one selected station and required confirmation, then POST to the **same** validated access activation endpoint. On errors, preserve the typed query/choice in an authorized scope; show visible validation messages. Keep recent-reauthentication flow valid without losing the chosen station.

## 7. Publication UI separation — exact behavior

### Department admins

- Remove the old publication card and Publish Now form from **File Management parent**.
- Exclude every department publication control from the **File Explorer child**.
- Use `publications-department-files-publish` (implemented Phase 1) in the existing Publications scope detail for the `department_files` dataset.
- Render this POST form **only** for that scope and authorized department admins:

```django
{% if scope.dataset_type_code == "department_files" and scope.station_id is None %}
  <form method="post"
        action="{% url 'publications-department-files-publish' scope.department_id %}"
        class="d-inline-block">
    {% csrf_token %}
    <button type="submit" class="btn btn-primary">Publish Now</button>
  </form>
  <p class="form-text">This prepares the next signed publication; it does not confirm tablet delivery.</p>
{% endif %}
```

- Reuse the existing scope lifecycle, history, signed publication, staging/rollback and authorization code. The Phase 1 endpoint uses the existing `publish_now()` service, recent reauth and job coalescing.
- Disallow the **old department** `POST action=publish` on `file-library` after moving its UI. This must be an authoritative role/scope check, not merely hiding the button. A station admin `action=publish` on their own station File Management parent remains supported.

**Station admins**

- They cannot access department-wide Publications.
- Keep **Station file publication** with current state and Publish Now on the station **parent** page only.
- The **same dedicated explorer subpage** is fully available for normal station-admin file operations in their own station. Never leave station admins on the legacy, form-heavy library page.
- Preserve access to their own capacity-review requests and retry flow from the station parent; they cannot approve or reject reviews as department admins.
- Do not make publication an explorer table row or folder-level operation; publication applies to the *library*, not a directory.
- Temporary department-admin access to a station library must never enable publish.

### Both

- File changes retain their existing dirty-state/publication scheduling semantics. Moving the UI must not change worker behavior, audit, signatures, source revisions, or live tablet delivery semantics.
- The Data Hub File Management **card retains its read-only publication summary**.

## 8. Important edge cases and non-negotiable behavior

- **Multi-library index:** `GET /files/` continues redirecting when exactly one authorized library exists; show a scope picker for multiple libraries.
- **Malformed folder UUID:** fail cleanly; never open library root as a silent fallback for a malformed nonempty identifier.
- **Foreign library/entry:** deny without revealing file names, station info or entry paths.
- **Recursive folder delete:** preserve existing explicit confirmation and single-service deletion; no client-side deletion without server acknowledgement.
- **Temporary grant expiry:** no continued browsing or delete privilege after expiry. One successful deletion consumes grant and redirects away from now unauthorized explorer.
- **POST/GET separation:** GETs are read-only except narrowly understood existing library-scope initialization; mutation endpoints accept POST and CSRF, not GET.
- **Modal retry:** invalid rename/move/upload/replace/confirm returns understandable inline errors; don't silently close dialog.
- **Rapid multiple uploads:** existing per-file results and capacity warning requests must remain identifiable; don't display an unconditional "All files uploaded" message.
- **Zero files:** helpful empty state but no fake demo content.
- **Browser history:** breadcrumbs/search/sort/pagination have real GET URLs; back button restores location.
- **Accessibility:** focus restoration after modal close, keyboard-reachable file actions, native anchors, visually hidden labels, status text not color alone, no auto-submitting destructive dropdown menu.
- **Security:** `operations`/booleans only govern display; all service methods reauthorize.
- **Performance:** `read_explorer()` defaults to 25 items/page, max 100. No query for all folders on each explorer render or each file row. The move modal may query one destination page at a time.
- **Presentation:** use repository-local SVGs and Bootstrap classes. No CDN, new JS framework, file-manager dependency or new client-side storage.
- **Language:** English interface strings only.

## 9. Implementation checklist (completed Phase 3 work)

1. Add explorer GET route and view using the existing `read_explorer()` read model; map validated query parameters to context.
2. Extract `templates/file_library/library.html` into a compact **shared parent** and create the **shared child** `explorer.html`; test both normal department and normal station entry paths before implementing modal actions.
3. Build `_explorer_listing.html`, `_explorer_row.html`, pagination and empty states; test browsing before mutations.
4. Implement one reusable modal action endpoint and forms for upload, new folder, rename, move, replace and delete. Reuse services and handle POST/fallback robustly.
5. Add single searchable station-access modal to parent; server-scoped search endpoint, accessible combobox controller and standard no-JS fallback.
6. Move the **department** publication control to existing Publications UI, retain station-only parent publication, and explicitly deny legacy department-file publish POST through File Management.
7. Preserve pending-capacity review links/counts on department parent, correct reauth/override behavior, active temporary-access banner and release.
8. Add parameterized server role/scope matrix tests covering **both normal department and normal station admins**, temporary department-admin access, system/unrelated actors, direct GET/POST/preview requests, HTTP/HTMX response protocols, station requester capacity reviews and bounded queries. Check modal behavior, accessibility markup and browser interactions manually outside pytest.
9. Update `docs/FireDash_UI_Styleguide.md` and `docs/file-libraries.md` describing final parent/child split.
10. Run focused PostgreSQL tests, Ruff, mypy, Django checks, migration-drift and whitespace verification. Full suite in GitHub CI once published.

## 10. Acceptance walkthrough

**Department admin**

1. From sidebar File Management, see parent scope, usage, pending capacity review link (if any), temporary station-access button and Open File Explorer.
2. Open explorer; browse root → nested folder → breadcrumb back; search nested PDF by filename and see ancestor path; sort; paginate.
3. Upload valid PDFs; create folder; rename; move; replace preserving document ID; delete with explicit confirmation. Verify audit and existing PDF sanitizer; test invalid PDF and an upload requiring tablet-capacity review.
4. Return to parent. No Publish Now card/button; Publications → Department files scope has the authorized publication action and history.
5. Open temporary station modal; search 120+ stations; choose by pointer or keyboard; confirm; reauthenticate; enter temporary station parent → Explorer; preview/download/delete; one deletion revokes grant.
6. A Department Admin must not gain unrestricted station file management through that temporary flow.

**Station admin**

1. Their sidebar File Management **and station Data Hub** lead to the same own-station parent, with Open File Explorer, own capacity-review links and compact Station file publication.
2. Open the **same new Explorer UI used by Department Administrators**, with the same breadcrumbs, folder navigation, search, sorting, paging, upload/new folder and PDF row menus/modals. Complete upload, rename, move, replace and confirmed delete in their own station.
3. Trigger a tablet-capacity warning on upload and verify the **requester-specific** station-parent review link, its status and the existing retry flow. They may request/retry, but not approve or reject the warning.
4. Publish station files from the parent only; the Explorer must show no publication controls. Cannot access department-wide Publications, other stations' parent/explorer/action URLs or the temporary-access picker.
5. Verify denial for cross-station and cross-department `file-library-explorer`, action-modal GET/POST, PDF preview and download, including manually crafted URL/hidden-field IDs.

**Temporary department-admin station access (distinct from normal station administration)**

1. Activate access from own department parent, enter selected station parent, then the **same Explorer** in restricted mode.
2. Confirm the File Explorer shows the station name and expiry, and permits folder browsing, PDF preview/download and a confirmed delete—but no upload, new folder, rename, move, replace, or publication controls.
3. Direct POST attempts to omitted actions are denied. One successful delete consumes the grant and returns to an authorized location; subsequent explorer/document reads are denied until a new grant. Failed delete does not consume access.
4. The temporary station parent shows no department approval queue and no normal station-admin publication section. Retain Release access and its audit semantics.

**System admin / unrelated tenant**

1. No illicit access to department/station file content or mutation routes. Existing navigation, system settings and audit UI unchanged.

**Keyboard / mobile**

1. Focusable visible controls, correct modal focus/escape, usable row action dropdowns, accessible combobox arrow/Enter/Escape behavior, no clipped action menus, safe no-JS fallback.

## 11. Canonical UI styleguide update (small, required)

The companion guide is implementation-specific; `docs/FireDash_UI_Styleguide.md` remains authoritative for global styling. The canonical styleguide now documents **File Management & File Explorer**, reflecting these stable rules (do not paste all ~1,000 lines into it):

> One shared File Management parent template and one shared File Explorer child template serve authorized Department and Station Administrators. Department admins manage their own department library and its publication lifecycle through Publications; normal station admins manage their own station library and publish station files from the station parent because they have no Publications access. Department admins accessing a station by temporary grant use the same explorer in browse/download/single-delete-only mode, with an explicit access banner and release. The parent hosts role-specific overview, review and access workflows; the child owns file/folder browsing and mutations. All permissions come from server-side scope checks; no duplicate station explorer and no role enforcement solely by hiding UI controls.

Document that the **department approval queue** and **station requester-review links** are separate, authorized workflows. The Data Hub's department file card remains read-only about publication. Keep the styleguide update concise and limited to implemented behavior; this companion guide provides the detailed HTML and accessibility contracts.

## 12. Do not implement

- Cloud-drive shares, sync, favorites, trash/recycle bin, bulk selection, ZIP downloads, comments, thumbnails, document editor or browser-side encryption.
- Extra publication models, bypasses for sanitization, duplicate storage logic or unsupported file types.
- A new File Explorer sidebar or multiple nested panels just to imitate desktop operating systems.
- Global form generation for every row, huge per-row folder select boxes, or raw JSON endpoint with unvalidated model mutation.

## 13. Documentation references

- FireDash UI styleguide: `docs/FireDash_UI_Styleguide.md` (repository authority).
- FireDash file-library architecture: `docs/file-libraries.md` (repository authority).
- Nextcloud Files user guide (design precedent): https://docs.nextcloud.com/server/stable/user_manual/en/files/access_webgui.html
- W3C WAI-ARIA combobox: https://www.w3.org/WAI/ARIA/apg/patterns/combobox/
- Bootstrap 5 modals: https://getbootstrap.com/docs/5.3/components/modal/
- Bootstrap 5 dropdowns: https://getbootstrap.com/docs/5.3/components/dropdowns/
- HTMX attributes and events: https://htmx.org/reference/

**Priority:** Correct file operations and authorization first; familiar and accessible UX second; fancy JavaScript enhancements last. The new page must feel familiar even with JS disabled.
