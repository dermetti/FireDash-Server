# Hydrants v1

Preferred batch format is GeoJSON FeatureCollection. CSV and JSON array are also accepted, UTF-8 (a UTF-8 BOM is accepted for CSV/JSON).  Maximum structured upload is 20 MiB and 20,000 rows/features.

CSV/JSON require exactly: `external_identifier`, `longitude`, `latitude`, `hydrant_type`, `diameter_mm`, `status`. `external_identifier`, longitude and latitude are required. Text values are strings; `diameter_mm` is a non-negative integer or empty/`null`; `hydrant_type` is optional; empty status becomes `ACTIVE`. Identifiers must be unique within the file and canonically identify a hydrant within its department.

GeoJSON must contain only `type` and `features`; every feature is exactly `Feature`, `Point` geometry, and properties `external_identifier`, `hydrant_type`, `diameter_mm`, `status`. Coordinates are WGS84/EPSG:4326 `[longitude, latitude]`; longitude is -180..180 and latitude is -90..90. FireDash never guesses coordinate order or CRS.

`MERGE` creates/updates listed IDs and leaves absent hydrants untouched. Batch-only `AUTHORITATIVE_SNAPSHOT` also marks active in-scope hydrants absent from the file inactive; its preview prominently shows the deactivation count. Invalid fields, unknown columns/properties, duplicate IDs, bad UTF-8, non-finite/out-of-range coordinates, or size limits reject the preview.
