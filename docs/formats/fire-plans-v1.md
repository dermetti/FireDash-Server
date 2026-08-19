# Fire Plans v1

For one document, upload one PDF with optional `external_identifier`, optional
`object_name`, optional `address`, optional `postal_code`, optional `city`, and
optional paired `longitude`/`latitude` (WGS84/EPSG:4326). A Fire Plan needs at
least one identity: a nonblank `external_identifier` is preferred; otherwise
the trimmed Unicode `address` is its exact fallback identity. FireDash does not
fuzzily normalize, transliterate, or case-fold addresses. Changing an
address-identity means a different Fire Plan; changing an address on a plan
with an External ID is metadata-only. A blank optional value preserves existing
curated metadata. Nonblank coordinate changes are shown in preview before
confirmation.

For many documents upload one ZIP containing exactly `fire-plans-manifest-v1.csv`
and the declared PDFs. The CSV columns are exactly
`external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,action`;
`action` is `upsert` or explicit `deactivate`. For either action, provide the
External ID or the fallback address. `filename` names only the ZIP member and
is never identity. Missing rows do nothing. PDF packages are limited to 256 MiB
compressed, 512 MiB expanded, and 250 documents. Each PDF inside a package must
still obey the individual PDF size limit (100 MiB).

Every new/replaced PDF passes FireDash quarantine, validation and sanitizer before canonical acceptance. ZIP traversal, absolute paths, symlinks, duplicate/undeclared members and missing declarations are rejected. `source_pdf_sha256` is the original upload hash; the accepted sanitized PDF hash is separate; publication ciphertext SHA-256 is a third, unrelated value. Same bytes under a new filename deduplicate; changed bytes with the same chosen identity replace that logical plan. Explicit deactivation retains the canonical row and excludes it only from future publications.
