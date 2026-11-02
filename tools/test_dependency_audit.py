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


def test_both_scans_run_after_timeout_without_exposing_diagnostics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    summary = tmp_path / "job-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    run = Mock(side_effect=subprocess.TimeoutExpired("audit", 240, stderr="secret"))
    monkeypatch.setattr(dependency_audit.subprocess, "run", run)
    dependency_audit.main()
    assert run.call_count == 2
    assert [call.args[0][4] for call in run.call_args_list] == [
        "requirements/base.txt",
        "requirements/dev.txt",
    ]
    assert summary.read_text().count("INCOMPLETE") == 2
    assert "secret" not in summary.read_text()
    assert Path("dependency-audit/summary.md").exists()
