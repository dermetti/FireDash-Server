"""Real synthetic PDFs; process execution is mocked unless explicitly marked native."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pikepdf
import pytest

from apps.reference_data import pdf_worker
from apps.reference_data.tests.test_pdf_validation import write_pdf


@pytest.fixture
def job(tmp_path, monkeypatch):
    source, output = tmp_path / "input.pdf", tmp_path / "sanitized.pdf"
    write_pdf(source)
    (tmp_path / "request.json").write_text(
        json.dumps(
            {
                "strict": True,
                "max_input_bytes": 1000000,
                "max_output_bytes": 1000000,
                "max_pages": 500,
            }
        )
    )
    monkeypatch.setattr("apps.reference_data.pdf_validation._validate_mime", lambda path: None)
    return source, output


def test_worker_checks_real_input_and_normalized_output(job, monkeypatch):
    source, output = job
    run = Mock(side_effect=lambda *a, **kw: shutil.copyfile(source, output))
    monkeypatch.setattr(pdf_worker.subprocess, "run", run)
    assert pdf_worker.process(source, output) == (
        f"OK {output.stat().st_size} 1 {hashlib.sha256(output.read_bytes()).hexdigest()}"
    )
    assert run.call_args.args[0][:4] == [
        "/usr/bin/qpdf",
        "--deterministic-id",
        "--object-streams=disable",
        "--warning-exit-0",
    ]
    assert run.call_args.kwargs["check"] is True
    assert run.call_args.kwargs["timeout"] == 45


@pytest.mark.parametrize("stage", ["input", "output"])
def test_positive_content_rejection_at_both_boundaries(job, monkeypatch, stage):
    source, output = job

    def action_pdf(path):
        with pikepdf.new() as document:
            document.add_blank_page()
            document.Root["/OpenAction"] = pikepdf.Dictionary(S=pikepdf.Name("/Launch"))
            document.save(path)

    if stage == "input":
        action_pdf(source)
    run = Mock(side_effect=lambda *a, **kw: action_pdf(output))
    monkeypatch.setattr(pdf_worker.subprocess, "run", run)
    assert pdf_worker.process(source, output) == "ERR content prohibited_pdf_content"
    assert run.call_count == (0 if stage == "input" else 1)


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(2, "/usr/bin/qpdf"),
        subprocess.CalledProcessError(3, "/usr/bin/qpdf"),
        subprocess.TimeoutExpired("/usr/bin/qpdf", 45),
        OSError("I/O failure"),
        MemoryError(),
    ],
)
def test_ambiguous_process_failures_never_become_content_rejection(job, monkeypatch, error):
    monkeypatch.setattr(pdf_worker.subprocess, "run", Mock(side_effect=error))
    with pytest.raises(type(error)):
        pdf_worker.process(*job)
    assert not (job[1].parent / "result.txt").exists()


@pytest.mark.parametrize("error", [MemoryError(), OSError("I/O failure")])
def test_native_parser_operational_failures_are_not_content_rejections(job, monkeypatch, error):
    monkeypatch.setattr(pikepdf, "open", Mock(side_effect=error))
    with pytest.raises(type(error)):
        pdf_worker.process(*job)


def test_strict_and_recovery_policy_are_preserved(job, monkeypatch):
    source, output = job
    content = source.read_bytes()
    start = content.index(b"startxref\n") + len(b"startxref\n")
    end = content.index(b"\n", start)
    source.write_bytes(content[:start] + b"0" + content[end:])
    run = Mock(side_effect=lambda *a, **kw: write_pdf(output))
    monkeypatch.setattr(pdf_worker.subprocess, "run", run)
    assert pdf_worker.process(source, output) == "ERR content malformed_pdf"
    run.assert_not_called()
    policy_path = output.parent / "request.json"
    policy = json.loads(policy_path.read_text())
    policy["strict"] = False
    policy_path.write_text(json.dumps(policy))
    assert pdf_worker.process(source, output).startswith("OK ")


def test_worker_refuses_root_before_inspection(monkeypatch):
    monkeypatch.setattr(pdf_worker.os, "geteuid", lambda: 0, raising=False)
    process = Mock()
    monkeypatch.setattr(pdf_worker, "process", process)
    with pytest.raises(RuntimeError, match="root"):
        pdf_worker.main()
    process.assert_not_called()


def test_successful_worker_publishes_bounded_metadata(job, monkeypatch):
    source, output = job
    monkeypatch.setattr(pdf_worker.os, "geteuid", lambda: 1001, raising=False)
    monkeypatch.setattr(pdf_worker, "INPUT", source)
    monkeypatch.setattr(pdf_worker, "OUTPUT", output)
    monkeypatch.setattr(
        pdf_worker.subprocess, "run", lambda *a, **kw: shutil.copyfile(source, output)
    )
    pdf_worker.main()
    result = (output.parent / "result.txt").read_bytes()
    assert result.startswith(b"OK ") and result.endswith(b"\n") and len(result) <= 128


def test_failed_worker_never_publishes_a_content_result(job, monkeypatch):
    source, output = job
    monkeypatch.setattr(pdf_worker.os, "geteuid", lambda: 1001, raising=False)
    monkeypatch.setattr(pdf_worker, "INPUT", source)
    monkeypatch.setattr(pdf_worker, "OUTPUT", output)
    monkeypatch.setattr(pdf_worker.subprocess, "run", Mock(side_effect=MemoryError()))
    with pytest.raises(MemoryError):
        pdf_worker.main()
    assert not (output.parent / "result.txt").exists()


@pytest.mark.parametrize("policy", [b"null", b"[]", b"{}", b"x" * 257])
def test_invalid_policy_fails_before_native_inspection(job, monkeypatch, policy):
    (job[1].parent / "request.json").write_bytes(policy)
    inspect = Mock()
    monkeypatch.setattr(pdf_worker, "validate_pdf", inspect)
    with pytest.raises(ValueError):
        pdf_worker.process(*job)
    inspect.assert_not_called()


def test_native_qpdf_pipeline_when_available(job, monkeypatch):
    qpdf = shutil.which("qpdf")
    if qpdf is None:
        pytest.skip("native qpdf executable unavailable; Debian/systemd acceptance remains pending")
    monkeypatch.setattr(pdf_worker, "QPDF", str(Path(qpdf)))
    assert pdf_worker.process(*job).startswith("OK ")


def test_missing_mime_dependency_is_infrastructure_failure(job, monkeypatch):
    import apps.reference_data.pdf_validation as policy

    # Exercise the real MIME function, independent of host libmagic availability.
    monkeypatch.undo()
    from unittest.mock import patch

    with patch.dict("sys.modules", {"magic": None}), pytest.raises(RuntimeError):
        policy.validate_pdf(
            job[0], max_input_bytes=1000000, max_output_bytes=1000000, max_pages=500
        )
