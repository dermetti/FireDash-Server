"""FireDash fake iPad external acceptance client.

This package is intentionally an *external* API client:

* no Django imports;
* no database access;
* no server-side service/model imports.

It exercises the same HTTPS + cryptographic contract the physical iPad uses,
so it can drive live adoption/reactivation/check-in/manifest/dataset testing
against a real FireDash deployment.

Run with either::

    python tools/fake_ipad.py adopt --server https://firedash.example.org --token '...'
    python -m tools.fake_ipad adopt --server https://firedash.example.org --token '...'
"""

__version__ = "2.0.0"
