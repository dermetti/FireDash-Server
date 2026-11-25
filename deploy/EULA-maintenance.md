# Maintainer verification of assembled EULA hashes

`deploy.lib.eula.assemble_agreement_set()` consumes only the immutable result of
`verify_document_set()`. It performs no I/O and returns both complete agreements
in memory, or raises `EULAError` without returning a partial set. Its frozen
`AssembledAgreementSet` exposes `german` and `english` (each with `content` bytes and
`sha256`), `canonical_bytes`, `agreement_set_sha256`, agreement metadata, and the
original verified input set as `provenance`. Content and provenance are excluded
from the assembled result's representation. Do not log their contents explicitly.

Assembly removes exactly the specified notice and its two LF characters, then
replaces the entire terminal Annex B placeholder block. All other template bytes
and all provider bytes remain unchanged. Annex A fields remain unfilled. The
agreement identity uses schema 1, agreement version, agreement revision, provider
revision, and both assembled language hashes. Canonical JSON uses sorted keys,
compact separators, ASCII escaping, UTF-8 encoding and no trailing newline.
Provenance is retained separately and is not part of that identity.

Provider signatures authenticate the provider documents. They do not sign the
assembled agreements or establish the identity or authority of a representative.
The Python result types are internal interfaces: constructing one manually is
not cryptographic verification. Production callers must obtain provenance from
`verify_document_set()`; there is no raw-provider assembly entry point.

## Approved Revision 2 comparison

This is an explicit maintainer operation, separate from automated tests. On a
controlled Linux host, use a trusted FireDash release checkout with the unchanged
release-local legal inputs, Python 3.11 or later, Debian-packaged Minisign, and
curl 8.4 or later. Run from that checkout's root. This procedure intentionally
retrieves the four manifest-pinned provider files over HTTPS. It uses temporary
storage for verification, assembles only in memory, and prints hashes and fixed
status labels. It writes no complete agreements and requires no signing key.
Do not enable debugging that captures local variables or dump result objects.

```bash
python3 - <<'PY'
from pathlib import Path
from deploy.lib.eula import EULAError, verify_document_set, assemble_agreement_set

try:
    result = assemble_agreement_set(verify_document_set(Path("legal")))
except EULAError as exc:
    raise SystemExit(f"Verification or assembly failed: {exc}") from None

if (result.agreement_version, result.agreement_revision, result.provider_revision) != (
    "1.0-beta", 2, 1
):
    raise SystemExit("Reference comparison requires agreement 1.0-beta revision 2, provider revision 1")

expected = {
    "de": "15188bd7ab716abccfccb5981f8066a966a7de82d4746464f5ef1274279e1aa1",
    "en": "7de85b1cd3c45716e62e0f8108c269b95c84ed102997d5bf3a292e229116f1d9",
    "set": "38bf58bd49fd8f70fcb64d46f7d7a77c548ef9b27853b0cbee91ee549cb61b56",
}
actual = {
    "de": result.german.sha256,
    "en": result.english.sha256,
    "set": result.agreement_set_sha256,
}
for name, value in actual.items():
    status = "MATCH" if value == expected[name] else "MISMATCH"
    print(f"{name}: {value} {status}")
raise SystemExit(0 if actual == expected else 1)
PY
```

These reference hashes are comparison data for this approved revision, not
constants in the general assembly algorithm or additions to the manifest.
Do not replace trusted inputs, normalize bytes, or bypass verification to resolve
a mismatch. Investigate the trusted release and assembly rules instead.

Automated assembly tests use synthetic identities, literal golden documents and
independently calculated hashes. They do not download provider documents or add
complete real agreements to the repository. Native Minisign, HTTPS and Unix
symlink verification remain separate Linux validation requirements until actually
executed successfully.
