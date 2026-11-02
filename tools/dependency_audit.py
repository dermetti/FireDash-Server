"""Bounded, informational CI audits; never interpret an incomplete scan as clean."""

import json
import os
import subprocess
import sys
from pathlib import Path


def summarize(report: Path, returncode: int | None) -> str:
    """Only pip-audit's successful, complete JSON results support vulnerability counts."""
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
        dependencies = data["dependencies"]
        if not isinstance(dependencies, list) or not dependencies:
            raise ValueError("Missing dependencies")
        affected = []
        count = 0
        for dependency in dependencies:
            if dependency.get("skip_reason"):
                raise ValueError("Skipped dependency")
            vulnerabilities = dependency["vulns"]
            if not isinstance(vulnerabilities, list):
                raise ValueError("Invalid vulnerabilities")
            if vulnerabilities:
                affected.append(dependency)
                count += len(vulnerabilities)
        if returncode != (1 if count else 0):
            raise ValueError("Incomplete or inconsistent scan")
        lines = [
            f"Completed: **{count} advisory entries across {len(affected)} affected packages**",
            f"({len(dependencies)} resolved packages scanned).",
        ]
        for dependency in affected:
            for vulnerability in dependency["vulns"]:
                fixes = ", ".join(vulnerability["fix_versions"]) or "not reported"
                lines.append(
                    f"- {dependency['name']} {dependency['version']}: "
                    f"{vulnerability['id']}; reported fixes: {fixes}"
                )
        return "\n".join(lines)
    except (OSError, ValueError, KeyError, TypeError):
        return (
            "**INCOMPLETE -- execution/resolution/advisory-service failure "
            "or skipped dependencies.** "
            "Vulnerability totals are unknown; this is not a clean audit. "
            f"Process result: {returncode if returncode is not None else 'timeout/start failure'}."
        )


def main() -> None:
    destination = Path("dependency-audit")
    destination.mkdir(exist_ok=True)
    sections = [
        "# Dependency advisory report",
        "Informational maintenance findings; quality checks run independently. "
        "Advisory entries can include aliases and overlap between scans. "
        "A completed scan is not proof of dependency security.",
    ]
    for name in ("base", "dev"):
        report = destination / f"{name}.json"
        report.unlink(missing_ok=True)
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip_audit",
                    "-r",
                    f"requirements/{name}.txt",
                    "--format",
                    "json",
                    "--output",
                    str(report),
                ],
                timeout=240,
                # Raw resolver diagnostics can contain configured index credentials.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            returncode = result.returncode
        except (OSError, subprocess.TimeoutExpired):
            returncode = None
        sections.extend([f"## requirements/{name}.txt", summarize(report, returncode)])
        # Publish only structured audit data, never raw resolver/environment logs.
        # Incomplete JSON may contain diagnostic strings: retain the summary only.
        if "INCOMPLETE" in sections[-1]:
            report.unlink(missing_ok=True)
    summary = "\n\n".join(sections) + "\n"
    (destination / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write(summary)


if __name__ == "__main__":
    main()
