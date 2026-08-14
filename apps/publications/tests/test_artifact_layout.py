from pathlib import Path

from django.conf import settings

REPO_ROOT = Path(__file__).resolve().parents[3]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"


def _read_service(name: str) -> str:
    return (SYSTEMD_DIR / name).read_text()


def test_publication_temp_root_resides_below_artifact_root():
    assert settings.PUBLICATION_ARTIFACT_TEMP_ROOT == settings.PUBLICATION_ARTIFACT_ROOT / ".tmp"


def test_worker_uses_a_single_publication_readwritepath():
    unit = _read_service("fire-publication-worker.service")
    read_write = [line for line in unit.splitlines() if line.startswith("ReadWritePaths=")]
    assert read_write == ["ReadWritePaths=/var/lib/fire-backend/publications"]
    assert "publications-tmp" not in unit


def test_worker_receives_private_publication_credentials():
    unit = _read_service("fire-publication-worker.service")
    assert "LoadCredential=publication-kek:" in unit
    assert "LoadCredential=publication-signing-key:" in unit


def test_web_service_does_not_receive_private_publication_credentials():
    unit = _read_service("fire-backend.service")
    load_lines = [line for line in unit.splitlines() if line.startswith("LoadCredential=")]
    assert not any("publication-kek" in line for line in load_lines)
    assert not any("publication-signing-key" in line for line in load_lines)
    assert any("publication-signing-public-key" in line for line in load_lines)


def test_worker_preserves_sandbox_hardening():
    unit = _read_service("fire-publication-worker.service")
    for directive in (
        "ProtectSystem=full",
        "RestrictSUIDSGID=true",
        "UMask=0077",
        "NoNewPrivileges=true",
    ):
        assert directive in unit
