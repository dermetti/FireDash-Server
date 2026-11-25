import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools import dependency_audit


@pytest.mark.parametrize("vulnerable", [False, True])
def test_completed_audit_counts_advisories(tmp_path, vulnerable):
    report = tmp_path / "result.json"
    report.write_text(
        json.dumps(
            {
                "dependencies": [
                    {
                        "name": "example",
                        "version": "1.0",
                        "vulns": [{"id": "TEST-1", "fix_versions": ["1.1"]}] if vulnerable else [],
                    }
                ]
            }
        )
    )
    summary = dependency_audit.summarize(report, int(vulnerable))
    assert f"{int(vulnerable)} advisory entries" in summary
    assert ("TEST-1" in summary) == vulnerable


@pytest.mark.parametrize(
    "data,code",
    [
        (None, None),
        ("invalid json", 1),
        ({"dependencies": []}, 0),
        ({"dependencies": [{"skip_reason": "unavailable"}]}, 0),
        ({"dependencies": [{"vulns": []}]}, 1),
    ],
)
def test_incomplete_audit_never_reports_zero(tmp_path, data, code):
    report = tmp_path / "result.json"
    if data is not None:
        report.write_text(json.dumps(data))
    summary = dependency_audit.summarize(report, code)
    assert "INCOMPLETE" in summary
    assert "0 advisory" not in summary


def test_all_scans_run_after_timeout_without_exposing_diagnostics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    summary = tmp_path / "job-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    run = Mock(side_effect=subprocess.TimeoutExpired("audit", 240, stderr="secret"))
    monkeypatch.setattr(dependency_audit.subprocess, "run", run)
    dependency_audit.main()
    assert run.call_count == 3
    assert [call.args[0][4] for call in run.call_args_list] == [
        "requirements/production.lock",
        "requirements/dev.txt",
        "deploy/acme/requirements.lock",
    ]
    assert summary.read_text().count("INCOMPLETE") == 3
    assert "--disable-pip" in run.call_args_list[0].args[0]
    assert "--disable-pip" not in run.call_args_list[1].args[0]
    assert "--disable-pip" in run.call_args_list[2].args[0]
    assert "secret" not in summary.read_text()
    assert Path("dependency-audit/summary.md").exists()


def test_separate_complete_and_incomplete_artifacts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def audit(args, **kwargs):
        assert kwargs["timeout"] == 240
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        report = Path(args[-1])
        if "requirements/dev.txt" in args:
            report.write_text('{"dependencies": []}')
            return Mock(returncode=0)
        vulnerabilities = (
            [{"id": "TEST-ACME", "fix_versions": ["2"]}] if "acme" in str(report) else []
        )
        report.write_text(
            json.dumps(
                {"dependencies": [{"name": "example", "version": "1", "vulns": vulnerabilities}]}
            )
        )
        return Mock(returncode=int(bool(vulnerabilities)))

    monkeypatch.setattr(dependency_audit.subprocess, "run", audit)
    dependency_audit.main()
    destination = Path("dependency-audit")
    assert (destination / "production.json").is_file()
    assert (destination / "acme.json").is_file()
    assert not (destination / "dev.json").exists()
    summary = (destination / "summary.md").read_text()
    assert summary.count("INCOMPLETE") == 1
    assert "0 advisory entries" in summary
    assert "1 advisory entries" in summary
    assert "TEST-ACME" in summary
