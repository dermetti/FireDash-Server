# KLGV Plans v1

KLGV Plans are imported as one ZIP containing `manifest.csv` and the referenced PDF files. The package is staged and reviewed before the explicit final Apply operation creates or updates canonical records.

`manifest.csv` columns are exactly:

`external_identifier,filename,object_name,address,postal_code,city,longitude,latitude,action`

`external_identifier` and both coordinates are optional. `object_name`, `address`, `postal_code`, and `city` are required for every `upsert` row. `filename` is a ZIP-member reference only and must identify exactly one PDF. `action` is `upsert` or `deactivate`.

The importer rejects ambiguous, missing, duplicate, unsafe, or extra PDF members. SHA-256, page count, canonical UUID, and storage path are derived by FireDash after PDF validation. Canonical paths are `plans/{uuid}.pdf`; uploaded filenames never become canonical paths.

Coordinates use EPSG:4326 decimal degrees and may remain empty. Review can correct missing values; FireDash never geocodes automatically. Accepted review decisions remain staged until final Apply.
