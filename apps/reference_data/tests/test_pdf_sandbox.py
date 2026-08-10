import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.reference_data.pdf_sandbox import PdfSanitizerError, PdfSanitizerTimeout, sanitize


@override_settings(PDF_SANITIZER_WRAPPER="/usr/local/lib/fire-backend/fire-pdf-sanitize")
def test_sanitizer_requires_linux_systemd_wrapper(tmp_path: Path) -> None:
    with patch("apps.reference_data.pdf_sandbox.sys.platform", "win32"):
        with pytest.raises(PdfSanitizerError):
            sanitize(
                quarantined_input=tmp_path / "input.pdf", sanitized_output=tmp_path / "output.pdf"
            )


def test_sanitizer_timeout_is_categorized(tmp_path: Path) -> None:
    with (
        patch("apps.reference_data.pdf_sandbox.sys.platform", "linux"),
        patch("apps.reference_data.pdf_sandbox.Path.is_file", return_value=True),
        patch("apps.reference_data.pdf_sandbox.os.access", return_value=True),
        patch(
            "apps.reference_data.pdf_sandbox.subprocess.run",
            side_effect=subprocess.TimeoutExpired("sandbox", 60),
        ),
    ):
        with pytest.raises(PdfSanitizerTimeout):
            sanitize(
                quarantined_input=tmp_path / "input.pdf", sanitized_output=tmp_path / "output.pdf"
            )


def test_sanitizer_uses_only_the_job_uuid_with_sudo(tmp_path: Path) -> None:
    job = "123e4567-e89b-12d3-a456-426614174000"
    quarantined_input = tmp_path / job / "input.pdf"
    sanitized_output = tmp_path / job / "sanitized.pdf"
    calls = []
    with (
        patch("apps.reference_data.pdf_sandbox.sys.platform", "linux"),
        patch("apps.reference_data.pdf_sandbox.Path.is_file", return_value=True),
        patch("apps.reference_data.pdf_sandbox.os.access", return_value=True),
        patch(
            "apps.reference_data.pdf_sandbox.subprocess.run",
            side_effect=lambda args, **_: calls.append(args),
        ),
    ):
        sanitize(quarantined_input=quarantined_input, sanitized_output=sanitized_output)
    assert calls == [
        ["/usr/bin/sudo", "-n", str(Path("/usr/local/lib/fire-backend/fire-pdf-sanitize")), job]
    ]
