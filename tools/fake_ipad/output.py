"""Human- and machine-readable output helpers for the fake iPad tool."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

_SECRET_KEYS = {
    "token",
    "credential",
    "challenge_response",
    "encrypted_challenge",
    "wrapped_content_key",
    "wrapped_generation_key",
    "hpke_private_key",
    "private_key",
    "cek",
    "kek",
    "generation_key",
    "authorization",
    "generation_wrapped_cek",
}


def redact(value: Any, key: str | None = None) -> Any:
    """Recursively redact secrets while keeping useful shape/length info."""
    if key and key.lower() in _SECRET_KEYS:
        return f"<redacted:{len(value)} chars>" if isinstance(value, str) else "<redacted>"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def pretty(value: Any) -> str:
    return json.dumps(redact(value), indent=2, ensure_ascii=False, sort_keys=True)


def text_indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


class Output:
    """Route diagnostics and emit a machine-readable result.

    In normal mode everything goes to stdout. In ``--json`` mode the human
    diagnostics are redirected to stderr so stdout carries only a single
    JSON document, which is what makes automation safe and deterministic.
    """

    def __init__(
        self, *, json_mode: bool = False, verbose: bool = False, stream: TextIO | None = None
    ) -> None:
        self.json_mode = json_mode
        self.verbose = verbose
        self.compact = False
        self._stream = stream or sys.stdout

    def line(self, message: str = "") -> None:
        if not self.compact or self.verbose:
            self.summary(message)

    def summary(self, message: str = "") -> None:
        target = sys.stderr if self.json_mode else self._stream
        print(message, file=target)

    def detail(self, message: str = "") -> None:
        if self.verbose:
            self.summary(message)

    def error(self, error: Exception) -> None:
        self.summary("\nSTATUS: FAIL")
        labels = {
            "dataset": "DATASET",
            "scope": "SCOPE",
            "publication": "Publication",
            "artifact": "Artifact",
            "document": "Document",
            "stage": "Stage",
            "http_status": "HTTP",
            "request_id": "Request ID",
        }
        context = getattr(error, "context", {})
        if context.get("stage") == "main_manifest":
            self.summary("Manifest: FAIL")
        for key, label in labels.items():
            if context.get(key) is not None:
                self.summary(f"{label}: {context[key]}")
        if "documents_total" in context:
            self.summary(
                f"Verified before failure: {context.get('documents_verified', 0)} "
                f"of {context['documents_total']} documents"
            )
        self.summary(f"Error: {error}")

    def banner(self, title: str) -> None:
        if self.compact and not self.verbose:
            if "RESULT:" in title or title.startswith("FIREDASH"):
                self.summary("\n" + title)
        else:
            self.line("\n" + "=" * 72)
            self.line(title)
            self.line("=" * 72)

    def emit(self, result: dict[str, Any]) -> None:
        if not self.json_mode:
            return
        print(json.dumps(redact(result), sort_keys=True, ensure_ascii=False), file=self._stream)

    def verification(
        self,
        manifest: dict,
        results: dict,
        *,
        cached: bool = False,
        installation_status: str | None = None,
    ) -> None:
        """Render validated data only; counters never depend on log messages."""
        names = {
            "dangerous_goods": "Dangerous Goods",
            "department_hydrants": "Hydrants",
            "station_personnel": "Personnel",
            "department_phonebook": "Department Phonebook",
            "station_phonebook": "Station Phonebook",
            "department_files": "Department Files",
            "station_files": "Station Files",
            "department_fire_plans": "Fire Plans",
            "department_klgv_plans": "KLGV Plans",
        }
        stats = verification_statistics(manifest, results, cached=cached)
        totals = stats["cached_verified"] if cached else stats["downloaded_verified"]
        if installation_status:
            self.summary(f"\nInstallation: {installation_status.upper()}")
        self.summary("Manifest:     " + ("UNCHANGED / PASS" if cached else "PASS (Ed25519, ETag)"))
        self.summary(f"Datasets:     {len(manifest['datasets'])}")
        for scope in totals:
            self.summary("\n" + scope)
            for dataset in sorted(
                manifest["datasets"], key=lambda d: names.get(d["type"], d["type"])
            ):
                if dataset["scope"]["type"] != scope:
                    continue
                kind = dataset["type"]
                result = results.get(kind)
                label = names.get(kind, kind)
                if result is None:
                    self.summary(f"  {label:<22} SKIPPED (not verified)")
                    continue
                metrics = []
                for key, unit in (
                    ("record_count", "records"),
                    ("eri_card_count", "ERI cards"),
                    ("documents", "PDFs"),
                    ("fire_plans", "PDFs"),
                    ("features", "features"),
                    ("people", "persons"),
                    ("entries", "entries"),
                ):
                    if key in result:
                        metrics.append(f"{result[key]:,} {unit}")
                status = "CACHED / PASS" if cached else "PASS"
                self.summary(f"  {label:<22} {status:<13} {' / '.join(metrics)}")
        self.summary(
            "\nDOWNLOAD SUMMARY" + (" (verified cache reused; no new downloads)" if cached else "")
        )
        for scope, count in totals.items():
            self.summary(
                f"  {scope:<12} {0 if cached else count:>6,} newly downloaded, verified artifacts"
            )
        self.summary(
            f"  TOTAL        {0 if cached else sum(totals.values()):>6,} "
            "newly downloaded, verified artifacts"
        )
        if cached:
            self.summary(
                f"  CACHE        {sum(totals.values()):>6,} previously verified artifacts reused"
            )
            self.summary("\nCryptography / Required datasets: PASS (verified snapshot)")
            self.summary(
                "Conditional GET: PASS (main manifest; artifact checks from verified snapshot)"
            )
        else:
            self.summary("\nCryptography: PASS")
            self.summary("Required datasets: PASS (all)")
            self.summary("Conditional GET: PASS")


def verification_statistics(manifest: dict, results: dict, *, cached: bool = False) -> dict:
    """Count completed results by signed scope, independent of rendering and HTTP logs.

    A v1 archive/JSON/GeoJSON is one artifact. Each completed v2 document is
    one artifact. A generation manifest and conditional responses count as zero.
    Failed generations never reach results, so cannot contribute any artifacts.
    """
    totals = dict.fromkeys(("SYSTEM", "DEPARTMENT", "STATION"), 0)
    for dataset in manifest["datasets"]:
        result = results.get(dataset["type"])
        if result is not None:
            count = (
                result["documents"] if dataset["artifact_format"] == "document-manifest-v2" else 1
            )
            totals[dataset["scope"]["type"]] += count
    return {
        "downloaded_verified": {s: 0 if cached else n for s, n in totals.items()},
        "cached_verified": {s: n if cached else 0 for s, n in totals.items()},
    }
