# PDF bundle v1 (historical compatibility format)

This specification describes the retained `pdf_bundles.py` builder/validator
contract and historical data, not an active registry delivery format or an
administrator upload format. No current production dataset advertises this
bundle. All four PDF datasets use schema-2
[Document Generation v2](../tablet-api.md#shared-document-generation-v2-contract)
with individual encrypted PDFs. Retain historical ciphertext unchanged; this
specification is not permission to reactivate or rewrite legacy generations.

The historical generated plaintext bundle is not an administrator upload format. It contains `manifest.json` and `documents/<uuid>.pdf`. The manifest has `schema_version: 1`, non-negative `source_revision`, and `documents`, deterministically ordered by lowercase UUID.

Each document has `id`, `title`, `path`, lowercase SHA-256 `sha256`, positive `page_count`, and optional nonempty `category`. `path` must be exactly `documents/<id>.pdf`; every declared member appears once, no undeclared members are allowed, and hashes must match bytes. ZIP traversal, backslashes, absolute paths, symlinks, duplicate members and more than 1,000 documents are rejected. Builders consume only already accepted sanitized PDFs.
