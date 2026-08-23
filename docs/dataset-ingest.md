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

Format references:

- [Hydrants v1](formats/hydrants-v1.md)
- [Personnel v1](formats/personnel-v1.md)
- [Fire Plans v1](formats/fire-plans-v1.md)
- [KLGV Plans v1](formats/klgv-plans-v1.md)
- [PDF bundle v1](formats/pdf-bundle-v1.md)
