import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from apps.reference_data.pdf_sandbox import (
    PdfSanitizerContentError,
    PdfSanitizerError,
    PdfSanitizerTimeout,
    sanitize,
)


def _job_id() -> str:
    return "123e4567-e89b-12d3-a456-426614174000"


def _mock_client(reply: bytes) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.recv.return_value = reply
    return client


def test_pdf_sandbox_does_not_use_sudo_or_subprocess() -> None:
    import apps.reference_data.pdf_sandbox as module

    source = Path(module.__file__).read_text()
    assert "sudo" not in source
    assert "subprocess" not in source


def test_sanitizer_requires_linux(tmp_path: Path) -> None:
    with patch("apps.reference_data.pdf_sandbox.sys.platform", "win32"):
        with pytest.raises(PdfSanitizerError):
            sanitize(
                quarantined_input=tmp_path / _job_id() / "input.pdf",
                sanitized_output=tmp_path / "output.pdf",
            )


@override_settings(PDF_SANITIZER_BROKER_SOCKET="/run/fire-pdf-sanitizer-broker/broker.sock")
def test_sanitizer_sends_only_job_uuid_over_broker_socket(tmp_path: Path) -> None:
    job = _job_id()
    client = _mock_client(b"OK\n")
    sent: list[bytes] = []
    client.sendall.side_effect = sent.append

    with (
        patch("apps.reference_data.pdf_sandbox.sys.platform", "linux"),
        patch("apps.reference_data.pdf_sandbox.Path.exists", return_value=True),
        patch("apps.reference_data.pdf_sandbox.Path.is_file", return_value=True),
        patch("apps.reference_data.pdf_sandbox.socket.socket", return_value=client) as socket_cls,
    ):
        sanitize(
            quarantined_input=tmp_path / job / "input.pdf",
            sanitized_output=tmp_path / job / "sanitized.pdf",
        )

    assert sent == [job.encode("ascii") + b"\n"]
    socket_cls.assert_called_once_with(getattr(socket, "AF_UNIX", 1), socket.SOCK_STREAM)
    client.connect.assert_called_once_with("/run/fire-pdf-sanitizer-broker/broker.sock")
    client.shutdown.assert_called_once_with(socket.SHUT_WR)


def test_sanitizer_timeout_is_categorized(tmp_path: Path) -> None:
    client = _mock_client(b"")
    client.recv.side_effect = TimeoutError("timed out")

    with (
        patch("apps.reference_data.pdf_sandbox.sys.platform", "linux"),
        patch("apps.reference_data.pdf_sandbox.Path.exists", return_value=True),
        patch("apps.reference_data.pdf_sandbox.Path.is_file", return_value=True),
        patch("apps.reference_data.pdf_sandbox.socket.socket", return_value=client),
    ):
        with pytest.raises(PdfSanitizerTimeout):
            sanitize(
                quarantined_input=tmp_path / _job_id() / "input.pdf",
                sanitized_output=tmp_path / _job_id() / "sanitized.pdf",
            )


def test_sanitizer_broker_failure_is_categorized(tmp_path: Path) -> None:
    client = _mock_client(b"ERR failed\n")

    with (
        patch("apps.reference_data.pdf_sandbox.sys.platform", "linux"),
        patch("apps.reference_data.pdf_sandbox.Path.exists", return_value=True),
        patch("apps.reference_data.pdf_sandbox.Path.is_file", return_value=True),
        patch("apps.reference_data.pdf_sandbox.socket.socket", return_value=client),
    ):
        with pytest.raises(PdfSanitizerContentError):
            sanitize(
                quarantined_input=tmp_path / _job_id() / "input.pdf",
                sanitized_output=tmp_path / _job_id() / "sanitized.pdf",
            )


def test_sanitizer_output_failure_is_infrastructure_fatal(tmp_path: Path) -> None:
    client = _mock_client(b"ERR output\n")

    with (
        patch("apps.reference_data.pdf_sandbox.sys.platform", "linux"),
        patch("apps.reference_data.pdf_sandbox.Path.exists", return_value=True),
        patch("apps.reference_data.pdf_sandbox.Path.is_file", return_value=True),
        patch("apps.reference_data.pdf_sandbox.socket.socket", return_value=client),
    ):
        with pytest.raises(PdfSanitizerError):
            sanitize(
                quarantined_input=tmp_path / _job_id() / "input.pdf",
                sanitized_output=tmp_path / _job_id() / "sanitized.pdf",
            )


def test_sanitizer_broker_timeout_status_is_categorized(tmp_path: Path) -> None:
    client = _mock_client(b"ERR timeout\n")

    with (
        patch("apps.reference_data.pdf_sandbox.sys.platform", "linux"),
        patch("apps.reference_data.pdf_sandbox.Path.exists", return_value=True),
        patch("apps.reference_data.pdf_sandbox.Path.is_file", return_value=True),
        patch("apps.reference_data.pdf_sandbox.socket.socket", return_value=client),
    ):
        with pytest.raises(PdfSanitizerTimeout):
            sanitize(
                quarantined_input=tmp_path / _job_id() / "input.pdf",
                sanitized_output=tmp_path / _job_id() / "sanitized.pdf",
            )


def test_sanitizer_rejects_non_uuid_job(tmp_path: Path) -> None:
    with patch("apps.reference_data.pdf_sandbox.sys.platform", "linux"):
        with pytest.raises(PdfSanitizerError):
            sanitize(
                quarantined_input=tmp_path / "not-a-uuid" / "input.pdf",
                sanitized_output=tmp_path / "output.pdf",
            )


def test_sanitizer_errors_when_broker_socket_missing(tmp_path: Path) -> None:
    with (
        patch("apps.reference_data.pdf_sandbox.sys.platform", "linux"),
        patch("apps.reference_data.pdf_sandbox.Path.exists", return_value=False),
    ):
        with pytest.raises(PdfSanitizerError):
            sanitize(
                quarantined_input=tmp_path / _job_id() / "input.pdf",
                sanitized_output=tmp_path / _job_id() / "sanitized.pdf",
            )


def test_sanitizer_errors_when_output_missing(tmp_path: Path) -> None:
    client = _mock_client(b"OK\n")

    with (
        patch("apps.reference_data.pdf_sandbox.sys.platform", "linux"),
        patch("apps.reference_data.pdf_sandbox.Path.exists", return_value=True),
        patch("apps.reference_data.pdf_sandbox.Path.is_file", return_value=False),
        patch("apps.reference_data.pdf_sandbox.socket.socket", return_value=client),
    ):
        with pytest.raises(PdfSanitizerError, match="did not produce"):
            sanitize(
                quarantined_input=tmp_path / _job_id() / "input.pdf",
                sanitized_output=tmp_path / _job_id() / "sanitized.pdf",
            )
