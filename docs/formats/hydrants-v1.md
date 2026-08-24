# Hydrants v1

Hydrant imports accept CSV or GeoJSON FeatureCollection. CSV accepts a UTF-8 BOM. A hydrant import is bounded by both byte size and record count: the upload may not exceed 20 MiB, a CSV may not exceed 20,000 rows, and a GeoJSON FeatureCollection may not exceed 50,000 features.

CSV requires exactly: `external_identifier`, `longitude`, `latitude`, `street`, `house_number`, `hydrant_type`, `diameter_mm`, `status`. `external_identifier`, longitude and latitude are required. Text values are strings; `street`, `house_number`, and `hydrant_type` are optional; `diameter_mm` is a non-negative integer or empty; empty status becomes `ACTIVE`. Identifiers must be unique within the file and canonically identify a hydrant within its department.

GeoJSON must contain only `type` and `features`; every feature is exactly `Feature`, `Point` geometry, and properties `external_identifier`, `street`, `house_number`, `hydrant_type`, `diameter_mm`, `status`. Coordinates are WGS84/EPSG:4326 `[longitude, latitude]`; longitude is -180..180 and latitude is -90..90. FireDash never guesses coordinate order or CRS.

`MERGE` creates/updates listed IDs and leaves absent hydrants untouched. Absence from an import never deactivates a hydrant; the only lifecycle deactivation is an explicit `status=INACTIVE` on the imported row for that stable `external_identifier`. Invalid fields, unknown columns/properties, duplicate IDs, bad UTF-8, non-finite/out-of-range coordinates, or size limits reject the preview.
