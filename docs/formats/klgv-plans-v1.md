# KLGV Plans v1

KLGV is disabled by default. A single document uses one PDF plus `external_id`, `title`, optional `category`. Batch input is a ZIP with `manifest.csv` and PDFs; the CSV columns are exactly `external_id,filename,title,category,action`, where action is `upsert` or explicit `deactivate`. `external_id` is the stable department-scoped identity; filename only identifies the ZIP member.

The same PDF safety path and ZIP limits as Fire Plans apply. Missing rows never deactivate a document. Explicit deactivation retains its canonical history and removes it from future optional KLGV bundles. When enabled, it uses the normal generic publication/encryption/signing/grant pipeline; it has no KLGV-specific cryptography.
