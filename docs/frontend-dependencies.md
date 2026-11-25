# Locally served frontend dependency inventory

Reviewed 2026-11-25 using versioned upstream distributions and OSV's npm advisory
records. No Node build or runtime CDN is used. File SHA-256 values below identify
actual shipped bytes, not merely upstream version labels.

| Dependency / asset | Version | Source | License | SHA-256 |
| --- | --- | --- | --- | --- |
| Bootstrap CSS | 5.3.3 | [npm distribution](https://registry.npmjs.org/bootstrap/5.3.3), `dist/css/bootstrap.min.css` | MIT | `3c8f27e6009ccfd710a905e6dcf12d0ee3c6f2ac7da05b0572d3e0d12e736fc8` |
| Bootstrap bundle JS | 5.3.3 | same distribution, `dist/js/bootstrap.bundle.min.js` | MIT | `0833b2e9c3a26c258476c46266e6877fc75218625162e0460be9a3a098a61c6c` |
| Alpine JS | 3.14.3 | [npm distribution](https://registry.npmjs.org/alpinejs/3.14.3), `dist/cdn.min.js` | MIT | `689f513978d11d69f4d33794f7296c9a586a2e55de79bb447cddbc3f474f9f07` |
| HTMX JS | 2.0.6 | [npm distribution](https://registry.npmjs.org/htmx.org/2.0.6), `dist/htmx.min.js` | 0BSD | `b6768eed4f3af85b73a75054701bd60e17cac718aef2b7f6b254e5e0e2045616` |
| Swagger UI bundle JS | 5.33.1 | [sidecar tag distribution metadata](https://github.com/tfranzel/drf-spectacular-sidecar/blob/2026.10.1/distributions.json), [npm distribution](https://registry.npmjs.org/swagger-ui-dist/5.33.1) | Apache-2.0 plus bundled notices | `050bc415ee7048dcd881682678f720264e7da5e373f7461d7c58c755305255f7` |
| Swagger standalone preset JS | 5.33.1 | same distribution | Apache-2.0 plus bundled notices | `5243d492e14505e0cab87ac8b0195d0e615943e651743b2b698450a46eb470be` |
| Swagger CSS | 5.33.1 | same distribution | Apache-2.0 | `1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321` |
| Swagger favicon | 5.33.1 | same distribution | Apache-2.0 | `3ed612f41e050ca5e7000cad6f1cbe7e7da39f65fca99c02e99e6591056e5837` |

All eight assets were byte-compared with the corresponding versioned npm
tarballs. The four Swagger assets are delivered by
`drf-spectacular-sidecar==2026.10.1` (BSD-3-Clause wrapper), whose wheel is
hash-pinned in the production lock. Django staticfiles discovers its assets;
existing collectstatic/Nginx serving is retained. Sidecar includes upstream
LICENSE and bundled JS license notices. Its Redoc 2.5.4 assets are present in the
package but no Redoc route is exposed or loaded by FireDash.

Bootstrap bundles Popper 2.11.8 (MIT); Alpine bundles Vue reactivity/shared 3.1.5
(MIT). Versions are supported by their upstream versioned
[Bootstrap lock](https://github.com/twbs/bootstrap/blob/v5.3.3/package-lock.json)
and [Alpine lock](https://github.com/alpinejs/alpine/blob/v3.14.3/package-lock.json).
Their bytes are covered by the enclosing file hashes. Swagger includes React and
React DOM 18.3.1 (MIT), DOMPurify 3.4.13 (Apache-2.0 OR MPL-2.0), qs 6.15.2
(BSD-3-Clause), and other supporting dependencies. The
[machine-readable inventory](frontend-inventory.json) records 90 bundled package
identities/versions/licenses from the shipped source maps matched to Swagger's
versioned build lock, aggregate asset hashes and the actual advisory matches.
This is practical bundle provenance, not a claim of a complete source audit or
that every package's upstream license obligations have been independently certified.

## Advisory review

Targeted OSV npm version queries returned no entries for Bootstrap 5.3.3,
Alpine 3.14.3, HTMX 2.0.6, Popper 2.11.8, Vue reactivity/shared 3.1.5 or
Swagger UI 5.33.1 itself. The 90-package Swagger bundle query **did** identify
eight entries (five low, three moderate) in two supporting packages; Python pip-audit does not
establish the safety of these embedded JavaScript packages. Exact IDs and
reported affected/fixed ranges are retained in the JSON inventory.

| Package | Advisory IDs | Reported fix | Applicability / disposition |
| --- | --- | --- | --- |
| DOMPurify 3.4.13 | GHSA-6688-9rhm-gjv2, GHSA-p98j-92pf-mc4p | 3.4.16 | Require IN_PLACE / DOM node sanitization and, for the latter, node-removing hooks. |
| DOMPurify 3.4.13 | GHSA-c6gr-qvv4-5v3f | 3.4.17 | Requires XHTML parser mode and additional SVG allowance. |
| DOMPurify 3.4.13 | GHSA-cg2g-xh64-v4q7, GHSA-mrv2-3pqr-j5jr | 3.4.17 | Require shadow-root DOM/template modes or IN_PLACE with node-removing hooks. |
| qs 6.15.2 | GHSA-4mjr-xmp4-gh2g, GHSA-x5fp-wj9c-mxmx | 6.16.0 | Malicious object stringify failure / nondefault comma-parsing array-limit bypass. |
| qs 6.15.2 | GHSA-6pq8-jhm2-2vcw | 6.16.1 | Nondefault comma stringify of objects with hostile toString can throw. |

The reviewed [Swagger Markdown implementation](https://github.com/swagger-api/swagger-ui/blob/v5.33.1/src/core/components/providers/markdown.jsx)
sanitizes strings, returns strings, and does not enable those DOMPurify modes.
Its hook adds link attributes and does not detach nodes. The listed DOMPurify
preconditions are therefore absent from this integration; this is source-based
applicability analysis, not browser exploit testing. FireDash serves its own
fixed schema and does not add sanitizer hooks or remote schema selection.

The qs advisories describe synchronous exceptions or client-side allocation;
this copy executes in the documentation browser, not a Django/Node server.
Adversarial Try-it-out input and all Swagger-client call paths have not been
exhaustively exercised. Retain these findings for release-owner review; do not
claim the bundle is advisory-free or independently proven unreachable. Reported
fixes are available in upstream packages, but sidecar's current pinned release
does not include them. A future sidecar release or separately reviewed asset
update can incorporate fixes without adding a JavaScript build system. No
unrelated frontend upgrades or ad hoc edits to compiled bundles are made here.

Advisory evidence: [OSV API](https://google.github.io/osv.dev/api/), individual
OSV/GHSA records linked in the JSON; upstream advisory pages for
[Bootstrap](https://github.com/twbs/bootstrap/security/advisories),
[Alpine](https://github.com/alpinejs/alpine/security/advisories),
[HTMX](https://github.com/bigskysoftware/htmx/security/advisories) and
[Swagger](https://github.com/swagger-api/swagger-ui/security/advisories).
Repeat version-specific advisory review at release time. Absence of reported
advisories is not proof of complete dependency security.
