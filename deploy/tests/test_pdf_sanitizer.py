"""Deployment contract checks; native systemd acceptance is a separate requirement."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_worker_is_installed_narrowly_with_debian_system_dependencies():
    bootstrap = (ROOT / "deploy/bootstrap-lxc.sh").read_text()
    for package in ("python3-pikepdf", "python3-magic", "qpdf"):
        assert package in bootstrap
        assert package in (ROOT / "deploy/apt-packages.txt").read_text().splitlines()
    assert "for helper in pdf_worker.py pdf_validation.py; do" in bootstrap
    assert 'install -m 0644 -o root -g root "$ROOT/apps/reference_data/$helper"' in bootstrap
    assert "install -d -o root -g root -m 0755 /usr/local/lib/fire-backend/pdf" in bootstrap
    assert (
        "/usr/bin/python3 -I -c 'import pikepdf, magic; assert callable(magic.from_file)'"
        in bootstrap
    )


def test_deployment_verification_requires_real_pipeline_smoke_as_backend():
    verify = (ROOT / "deploy/verify-deployment.sh").read_text()
    assert (
        'runuser -u fire_backend -- "$RELEASE/venv/bin/python" "$RELEASE/manage.py" '
        "verify_library_pdf_sanitizer" in verify
    )
    assert 'fail "native PDF pipeline smoke check failed"' in verify
    assert '"root:root:644"' in verify
    assert '"root:root:755"' in verify
    assert "import pikepdf, magic" in verify


def test_debian_ci_exercises_distribution_worker_as_non_root():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "debian:13-slim" in workflow
    assert "python3-pikepdf python3-magic qpdf" in workflow
    assert "dpkg-query -W python3-pikepdf python3-magic qpdf" in workflow
    assert (
        "runuser -u pdf_worker_test -- /usr/bin/python3 -I "
        "/source/deploy/tests/native_pdf_worker.py" in workflow
    )
    assert (
        "install -o root -g root -m 0644 /source/apps/reference_data/pdf_worker.py "
        "/source/apps/reference_data/pdf_validation.py /usr/local/lib/fire-backend/pdf/" in workflow
    )
