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
    assert "LoadCredential=publication-signing-public-key-ring:" in unit
    for setting in ("NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=full"):
        assert setting in unit


def test_worker_credentials_are_separate_from_the_web_public_key_credential():
    delivery = _unit("fire-publication-delivery.service")
    build = _unit("fire-publication-build.service")
    web = _unit("fire-backend.service")
    for unit in (delivery, build):
        assert "LoadCredential=publication-kek:" in unit
        assert "LoadCredential=publication-signing-key:" in unit
        assert "LoadCredential=publication-signing-public-key-ring:" in unit
    assert "publication-kek" not in web
    assert "publication-signing-key:" not in web
    assert "LoadCredential=publication-signing-public-key-ring:" in web


def test_build_service_socket_and_timer_have_narrow_nightly_contract():
    build = _unit("fire-publication-build.service")
    socket = _unit("fire-publication-build.socket")
    timer = _unit("fire-publication-build.timer")
    assert "Type=oneshot" in build
    assert "[Install]" not in build
    assert "process_publication_jobs --build" in build
    assert "SupplementaryGroups=fire_document_readers" in build
    assert "--delivery" not in build
    assert "LoadCredential=publication-kek:" in build
    assert "LoadCredential=publication-signing-key:" in build
    assert "LoadCredential=publication-signing-public-key-ring:" in build
    assert "ListenStream=/run/fire-backend/publication-build.sock" in socket
    assert "FileDescriptorName=publication-build-wake" in socket
    assert "SocketGroup=fire_backend" in socket
    assert "SocketMode=0660" in socket
    assert "Service=fire-publication-build.service" in socket
    assert "OnCalendar=*-*-* 00:05:00" in timer
    assert "Unit=fire-publication-build.service" in timer


def test_maintenance_units_are_separated_by_service_identity_and_legacy_worker_is_retired():
    maintenance = _unit("fire-publication-maintenance.service")
    staging_maintenance = _unit("fire-import-staging-maintenance.service")
    staging_timer = _unit("fire-import-staging-maintenance.timer")
    installer = (ROOT / "deploy" / "lib" / "systemd.sh").read_text(encoding="utf-8")
    assert "publication-kek" not in maintenance
    assert "publication-signing-key" not in maintenance
    assert "[Install]" not in maintenance
    assert "cleanup_signed_manifests" in maintenance
    assert "cleanup_orphan_artifacts" in maintenance
    assert "cleanup_import_staging" not in maintenance
    assert "ReadWritePaths=/var/lib/fire-backend/import-staging" not in maintenance
    assert "User=fire_backend" in staging_maintenance
    assert "Group=fire_backend" in staging_maintenance
    assert "cleanup_import_staging" in staging_maintenance
    assert "ReadWritePaths=/var/lib/fire-backend/import-staging" in staging_maintenance
    assert "ReadWritePaths=/var/lib/fire-backend/publications" not in staging_maintenance
    assert "OnBootSec=10min" in staging_timer
    assert "OnUnitActiveSec=1d" in staging_timer
    assert "Unit=fire-import-staging-maintenance.service" in staging_timer
    assert "disable --now fire-publication-worker.timer" in installer
    assert "stop fire-publication-worker.service" in installer
    assert "enable --now fire-publication-delivery.service" in installer
    assert "enable --now fire-publication-build.socket" in installer
    assert 'enable --now "$timer"' in installer
    assert "fire-import-staging-maintenance.timer" in installer
    assert "fire-import-staging-maintenance.service" in installer


def test_verifier_checks_lane_commands_socket_security_and_credential_separation():
    verifier = (ROOT / "deploy" / "verify-deployment.sh").read_text(encoding="utf-8")
    assert "fire-publication-delivery.service active" in verifier
    assert "--delivery --forever --poll-seconds 2" in verifier
    assert "fire-publication-build.socket active" in verifier
    assert "publication build timer is scheduled nightly at 00:05" in verifier
    assert "root:fire_backend:660" in verifier
    assert "FileDescriptorName=publication-build-wake" in verifier
    assert "publication-signing-public-key-ring.json" in verifier
    assert "private/public pair matches the retained public-key ring" in verifier
    assert "maintenance service does not load KEK/private signing key" in verifier
    assert "import staging maintenance runs as fire_backend with narrow staging access" in verifier
    assert "publication maintenance has no import staging access" in verifier
    assert "fire_publication cannot modify import staging" in verifier
    assert "fire_backend has no passwordless sudo privilege" in verifier
    assert "fire_publication is outside broad fire_backend group" in verifier
    assert "fire_publication can read accepted source" in verifier
    assert "fire_publication cannot modify/delete accepted source" in verifier
    assert "www-data cannot read accepted source" in verifier


def test_accepted_document_source_uses_its_own_reader_group():
    users = (ROOT / "deploy" / "scripts" / "create-service-users.sh").read_text(encoding="utf-8")
    tmpfiles = (ROOT / "deploy" / "systemd" / "fire-backend.tmpfiles.conf").read_text(
        encoding="utf-8"
    )
    assert "groupadd --system fire_document_readers" in users
    assert "usermod -a -G fire_document_readers fire_backend" in users
    assert "usermod -a -G fire_document_readers fire_publication" in users
    assert "gpasswd -d fire_publication fire_backend" in users
    assert "fire_backend:fire_document_readers" in users
    assert "chmod 2750" in users and "chmod 0640" in users
    assert "2750 fire_backend fire_document_readers" in tmpfiles
    release = (ROOT / "deploy" / "lib" / "release.sh").read_text(encoding="utf-8")
    assert '"$root/deploy/scripts/create-service-users.sh"' in release


def test_root_rotation_helper_uses_two_phase_atomic_public_ring_workflow():
    helper = (ROOT / "deploy" / "rotate-publication-signing-key").read_text(encoding="utf-8")
    secrets = (ROOT / "deploy" / "lib" / "secrets.sh").read_text(encoding="utf-8")
    assert "require_root" in helper
    assert "prepare|activate" in helper
    assert "publication-signing-key-staging" in helper
    assert "replace_file_atomically" in helper
    assert 'replace_file_atomically "$PREPARE_CANDIDATE" "$RING_FILE" 0600 root:root' in helper
    assert 'if ! mv -f -- "$temporary" "$destination"; then' in helper
    assert 'rm -f -- "$temporary"' in helper
    assert "prepare validation failed" in helper
    assert "PUBLICATION_SIGNING_KEY_VERSION" in helper
    assert "systemctl stop fire-publication-delivery.service" in helper
    assert "systemctl enable --now fire-publication-build.socket" in helper
    assert "systemctl enable --now fire-publication-delivery.service" in helper
    assert "private_bytes_raw" in helper
    assert "public_key().public_bytes_raw" in helper
    # The operator-facing output is fingerprints/version metadata only.
    assert "private signing material" not in helper
    # Exact-SHA installer convergence preserves, rather than recreates, a
    # rotated ring and merely reconciles the active version entry.
    assert "if ring_path.exists()" in secrets
    assert "keys[active_version] = active_encoded" in secrets
