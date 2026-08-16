"""Static deployment regression tests for the three publication worker lanes."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _unit(name: str) -> str:
    return (ROOT / "deploy" / "systemd" / name).read_text(encoding="utf-8")


def test_delivery_unit_is_persistent_delivery_only_and_hardened():
    unit = _unit("fire-publication-delivery.service")
    assert "Type=simple" in unit
    assert "[Install]" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "--delivery --forever --poll-seconds 2" in unit
    assert "--build" not in unit
    assert "LoadCredential=publication-kek:" in unit
    assert "LoadCredential=publication-signing-key:" in unit
    for setting in ("NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=full"):
        assert setting in unit


def test_worker_credentials_are_separate_from_the_web_public_key_credential():
    delivery = _unit("fire-publication-delivery.service")
    build = _unit("fire-publication-build.service")
    web = _unit("fire-backend.service")
    for unit in (delivery, build):
        assert "LoadCredential=publication-kek:" in unit
        assert "LoadCredential=publication-signing-key:" in unit
    assert "publication-kek" not in web
    assert "publication-signing-key:" not in web
    assert "LoadCredential=publication-signing-public-key:" in web


def test_build_service_socket_and_timer_have_narrow_nightly_contract():
    build = _unit("fire-publication-build.service")
    socket = _unit("fire-publication-build.socket")
    timer = _unit("fire-publication-build.timer")
    assert "Type=oneshot" in build
    assert "[Install]" not in build
    assert "process_publication_jobs --build" in build
    assert "--delivery" not in build
    assert "LoadCredential=publication-kek:" in build
    assert "LoadCredential=publication-signing-key:" in build
    assert "ListenStream=/run/fire-backend/publication-build.sock" in socket
    assert "SocketGroup=fire_backend" in socket
    assert "SocketMode=0660" in socket
    assert "Service=fire-publication-build.service" in socket
    assert "OnCalendar=*-*-* 00:05:00" in timer
    assert "Unit=fire-publication-build.service" in timer


def test_maintenance_is_credential_free_and_legacy_worker_is_retired_by_installer():
    maintenance = _unit("fire-publication-maintenance.service")
    installer = (ROOT / "deploy" / "lib" / "systemd.sh").read_text(encoding="utf-8")
    assert "publication-kek" not in maintenance
    assert "publication-signing-key" not in maintenance
    assert "[Install]" not in maintenance
    assert "cleanup_signed_manifests" in maintenance
    assert "cleanup_orphan_artifacts" in maintenance
    assert "disable --now fire-publication-worker.timer" in installer
    assert "stop fire-publication-worker.service" in installer
    assert "enable --now fire-publication-delivery.service" in installer
    assert "enable --now fire-publication-build.socket" in installer
    assert 'enable --now "$timer"' in installer


def test_verifier_checks_lane_commands_socket_security_and_credential_separation():
    verifier = (ROOT / "deploy" / "verify-deployment.sh").read_text(encoding="utf-8")
    assert "fire-publication-delivery.service active" in verifier
    assert "--delivery --forever --poll-seconds 2" in verifier
    assert "fire-publication-build.socket active" in verifier
    assert "publication build timer is scheduled nightly at 00:05" in verifier
    assert "root:fire_backend:660" in verifier
    assert "maintenance service does not load KEK/private signing key" in verifier
    assert "fire_backend has no passwordless sudo privilege" in verifier
