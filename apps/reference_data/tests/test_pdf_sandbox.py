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
