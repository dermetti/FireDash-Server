# HPKE Dependency Review

## Decision

FireDash uses the native RFC 9180 HPKE implementation in `cryptography==50.0.0`.
No handwritten HPKE implementation is permitted.

## Selected Implementation

`cryptography==50.0.0` provides a native RFC 9180 single-shot HPKE API. FireDash
uses only this suite:

```text
DHKEM(P-256, HKDF-SHA256) / HKDF-SHA256 / AES-128-GCM
```

The fixed-suite wrapper in `apps/publications/hpke.py` permits only P-256 public
keys serialized as 65-byte SEC1 uncompressed points. It binds FireDash's canonical
publication, installation, tablet, and scope context through the library `info`
parameter. `pyhpke==0.6.5` is rejected and is not a project dependency.

## Validation

- `apps/publications/tests/fixtures/hpke_contract.json` freezes the canonical
  FireDash context encoding and an RFC 9180 P-256 base-mode vector.
- `apps/publications/tests/test_hpke.py` validates the RFC 9180 vector's P-256
  key encodings, Python round trips, canonical key encoding/fingerprinting, and
  failures on wrong keys, altered encapsulated keys/ciphertexts, and changed bound
  context values. The native single-shot API exposes `info` but not RFC 9180 AAD;
  the official base-mode vector's `Count-0` AAD encryption cannot be executed by
  this API. Full official-vector execution remains an acceptance blocker.
- Swift-to-Python and Python-to-Swift execution requires the tablet Swift test
  client and a Swift/Xcode runner. Neither is present in this workspace, so that
  deployment gate remains open and must not be recorded as passed.

This closeout adds only non-persistent HPKE primitives. App-installation persistence,
adoption, persistent key grants, manifests, and download APIs remain Phase 8/9 work.
