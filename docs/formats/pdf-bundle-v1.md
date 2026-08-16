# PDF bundle v1

This is the generated plaintext publication bundle, not an administrator upload format. It contains `manifest.json` and `documents/<uuid>.pdf`. The manifest has `schema_version: 1`, non-negative `source_revision`, and `documents`, deterministically ordered by lowercase UUID.

Each document has `id`, `title`, `path`, lowercase SHA-256 `sha256`, positive `page_count`, and optional nonempty `category`. `path` must be exactly `documents/<id>.pdf`; every declared member appears once, no undeclared members are allowed, and hashes must match bytes. ZIP traversal, backslashes, absolute paths, symlinks, duplicate members and more than 1,000 documents are rejected. Builders consume only already accepted sanitized PDFs.
