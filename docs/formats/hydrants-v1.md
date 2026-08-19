# Hydrants v1

Preferred batch format is GeoJSON FeatureCollection. CSV and JSON array are also accepted, UTF-8 (a UTF-8 BOM is accepted for CSV/JSON).  A hydrant import is bounded by both byte size and record count: the upload may not exceed 20 MiB, a CSV/JSON array may not exceed 20,000 rows, and a GeoJSON FeatureCollection may not exceed 50,000 features.

CSV/JSON require exactly: `external_identifier`, `longitude`, `latitude`, `hydrant_type`, `diameter_mm`, `status`. `external_identifier`, longitude and latitude are required. Text values are strings; `diameter_mm` is a non-negative integer or empty/`null`; `hydrant_type` is optional; empty status becomes `ACTIVE`. Identifiers must be unique within the file and canonically identify a hydrant within its department.

GeoJSON must contain only `type` and `features`; every feature is exactly `Feature`, `Point` geometry, and properties `external_identifier`, `hydrant_type`, `diameter_mm`, `status`. Coordinates are WGS84/EPSG:4326 `[longitude, latitude]`; longitude is -180..180 and latitude is -90..90. FireDash never guesses coordinate order or CRS.

`MERGE` creates/updates listed IDs and leaves absent hydrants untouched. Absence from an import never deactivates a hydrant; the only lifecycle deactivation is an explicit `status=INACTIVE` on the imported row for that stable `external_identifier`. Invalid fields, unknown columns/properties, duplicate IDs, bad UTF-8, non-finite/out-of-range coordinates, or size limits reject the preview.
