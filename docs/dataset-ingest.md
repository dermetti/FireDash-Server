# Dataset ingestion

FireDash imports canonical operational data through a preview-and-confirm workflow.  Use **Add one** for a single record or **Import many** for a file/package; both follow the same rules.

`input -> validation -> preview -> confirmation -> canonical database mutation -> affected scopes marked dirty -> existing publication pipeline`

An applied import does **not** directly create a DatasetPublication, ciphertext, signature, HPKE grant, or SignedManifest.  Those are later produced by the normal protected publication pipeline.

Each preview is bound to the exact staged upload SHA-256 and the canonical state observed during validation.  Confirmation rechecks both.  A changed/missing upload or stale preview must be recreated; confirmation is atomic and applies at most once.  Preview can be cancelled without changing canonical data.

Department Administrators can work only in their department; they may import personnel for any active station in that department.  Audit records retain safe batch metadata and counts, not raw personnel/PDF content.  Staged uploads are private and removed by maintenance after the configured preview/applied retention periods.

For Fire Plans, upload either one PDF with its metadata or one ZIP containing
`fire-plans-manifest-v1.csv` and the declared PDFs. Its exact header is
`external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,fsd_location,bmz_location,rwa_info,action`.
A nonblank External ID is the preferred
identity. If no External ID exists, the exact trimmed address is the fallback
identity; it is not fuzzy-matched. Filename is ZIP transport metadata only.
An omitted ZIP row never deactivates a Fire Plan.

KLGV Plans use one ZIP containing `manifest.csv` plus the declared PDFs. Their
current metadata contract is `external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,action`.
Object name, address, postal code, and city are required; coordinates are optional.
PDF hash, page count, canonical UUID, and `plans/{uuid}.pdf` storage path are
derived only after FireDash validates the accepted PDF. Personnel imports use
the five-column CSV contract documented below; older four-column input is rejected.

Format references:

- [Hydrants v1](formats/hydrants-v1.md)
- [Personnel v1](formats/personnel-v1.md)
- [Fire Plans v1](formats/fire-plans-v1.md)
- [KLGV Plans v1](formats/klgv-plans-v1.md)
- [Stations and Vehicles v1](formats/station-vehicles-v1.md)

## Import formats versus generated delivery

Versioned files above describe administrator input, not tablet ciphertext.
Hydrants support CSV/GeoJSON Merge only (manual Add one uses the same service);
absence does not deactivate. Personnel and Station/Vehicle CSV use staged Upsert.
Phonebook CSV uses exact header
`first_name,last_name,organization_unit,function,phone_number,scope` and staged
Upsert in the authorized department/station context; see the downloadable
[phonebook template](../apps/ingestion/static/ingestion/templates/phonebook-v1.csv).
Fire Plan/KLGV ZIP manifests are transport input and never become the tablet
publication format. Fire Plans also accept one PDF with form metadata; all PDFs
still pass the isolated sanitizer. Use exact column sets from the linked versioned
specifications/templates and review errors before applying.

Dangerous Goods is implemented: System administrators upload one curated UTF-8
`dangerous_goods_v1.json`, preview validation, then explicitly apply. The validator
requires `dataset_type:dangerous_goods`, schema 1, compact publication profile,
consistent counts, unique goods IDs, four-digit UN strings, valid ADR/ERI references and
placard catalog semantics (7X means exactly 7A/7B/7C). Validated original bytes
are retained verbatim, not normalized or enriched with invented classifications.
It publishes once as an explicitly exposed SYSTEM encrypted-JSON dataset;
Department administrators consume it read-only. The detailed
[Dangerous Goods wire/validation contract](tablet-api.md#dangerous-goods-dangerous_goods-artifact_format-json)
is separate from global Vehicle Rescue Guides URL configuration.

Current generated dataset schemas/formats are listed in the
[Tablet API inventory](tablet-api.md#document-snapshot-caching-and-supported-dataset-inventory).
All four document datasets are schema-2 `document-manifest-v2`; JSON/GeoJSON
reference datasets remain schema 1. The retained
[PDF bundle v1 specification](formats/pdf-bundle-v1.md) is historical compatibility
only, with no active production registry delivery or administrator intake path.
The [adoption HPKE v1 contract](formats/adoption-hpke-v1.md) remains active and
independent of document-generation schema versions.
