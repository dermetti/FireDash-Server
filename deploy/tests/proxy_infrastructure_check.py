"""Deployment regression scan with one exact, release-approved declaration exception."""

import re
import sys
from pathlib import Path

PROHIBITED = re.compile(r"100\.64\.0\.8|mjblab|squid|tailscale|headscale", re.IGNORECASE)


def contains_prohibited(root: Path) -> bool:
    library = root / "deploy/lib"
    paths = [path for path in library.rglob("*") if path.is_file()]
    paths.extend(
        root / name
        for name in ("deploy/install-local.sh", "docs/configuration.md", "docs/deployment.md")
    )
    for path in paths:
        # Python bytecode is not deployment source (local test runs can create it).
        if path.parent.name == "__pycache__" and path.suffix == ".pyc":
            continue
        declaration_seen = False
        for line in path.read_text(encoding="utf-8").splitlines():
            if (
                path == library / "eula.py"
                and line == 'ORIGIN = "https://firedash-web.mjblab.de"'
                and not declaration_seen
            ):
                declaration_seen = True
                continue
            if PROHIBITED.search(line):
                return True
    return False


if __name__ == "__main__":
    try:
        prohibited = contains_prohibited(Path(sys.argv[1]))
    except (OSError, UnicodeError):
        raise SystemExit(2) from None
    raise SystemExit(0 if prohibited else 1)
