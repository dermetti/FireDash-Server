#!/usr/bin/env python3
"""Compatibility entry point.

The fake iPad client now lives in the ``tools/fake_ipad/`` package. This shim
keeps ``python tools/fake_ipad.py ...`` working exactly as before; prefer
``python -m tools.fake_ipad ...`` when running from the repository root.

Usage::

    python tools/fake_ipad.py adopt --server https://firedash.example.org --token '...'
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.fake_ipad.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
