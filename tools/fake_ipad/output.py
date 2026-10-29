"""Human- and machine-readable output helpers for the fake iPad tool."""

from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import contextmanager
from typing import Any, TextIO

_SECRET_KEYS = {
    "protected",
    "encrypted_key",
    "iv",
    "ciphertext",
    "tag",
    "email",
    "recipient_email",
    "magic_link",
    "link_url",
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

_DATASET_NAMES = {
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
_SCOPES = ("SYSTEM", "DEPARTMENT", "STATION")


def ordered_datasets(manifest: dict) -> list[dict]:
    """The display order is also the actual verification order."""
    return sorted(
        manifest["datasets"],
        key=lambda d: (_SCOPES.index(d["scope"]["type"]), _DATASET_NAMES.get(d["type"], d["type"])),
    )


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
        self._lock = threading.RLock()
        self._scope: str | None = None
        self._active_line: str | None = None
        self._line_width = 0

    def line(self, message: str = "") -> None:
        if not self.compact or self.verbose:
            self.summary(message)

    def summary(self, message: str = "") -> None:
        target = sys.stderr if self.json_mode else self._stream
        with self._lock:
            # Even unexpected diagnostics cannot interleave with a spinner frame.
            if self._active_line is not None:
                self._rewrite_line("")
            print(message, file=target, flush=True)
            if self._active_line is not None:
                self._rewrite_line(self._active_line)

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
        with self._lock:
            print(
                json.dumps(redact(result), sort_keys=True, ensure_ascii=False),
                file=self._stream,
                flush=True,
            )

    def _rewrite_line(self, text: str) -> None:
        """Carriage return and padding work in PowerShell without ANSI/VT setup."""
        try:
            columns = os.get_terminal_size(self._stream.fileno()).columns
        except (AttributeError, OSError, ValueError):
            columns = 80
        width = max(0, columns - 1)
        text = text[:width]
        self._line_width = min(width, max(self._line_width, len(text)))
        print("\r" + text.ljust(self._line_width) + "\r", end="", file=self._stream, flush=True)

    def dataset_scope(self, dataset: dict) -> None:
        scope = dataset["scope"]["type"]
        if scope != self._scope:
            self.summary("\n" + scope)
            self._scope = scope

    @contextmanager
    def dataset_progress(self, dataset: dict):
        """Own and join the animation thread, including on BaseException exits."""
        self.dataset_scope(dataset)
        animate = not self.verbose and not self.json_mode and self._stream.isatty()
        stop = threading.Event()
        thread = None
        if animate:
            frames = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
            try:
                frames.encode(getattr(self._stream, "encoding", None) or "utf-8")
            except UnicodeEncodeError:
                frames = "|/-\\"
            label = _DATASET_NAMES.get(dataset["type"], dataset["type"])

            def frame(index: int) -> None:
                with self._lock:
                    self._active_line = (
                        f"  {label:<22} {frames[index % len(frames)]} RUNNING "
                        "- downloading and verifying..."
                    )
                    self._rewrite_line(self._active_line)

            def spin() -> None:
                index = 1
                while not stop.wait(0.1):
                    try:
                        frame(index)
                    except (OSError, ValueError):
                        stop.set()
                    index += 1

        try:
            if animate:
                frame(0)
                thread = threading.Thread(target=spin, name="fake-ipad-spinner", daemon=True)
                thread.start()
            yield
        except BaseException:
            self._stop_progress(stop, thread)
            self.dataset_result(dataset, status="FAIL")
            raise
        finally:
            self._stop_progress(stop, thread)

    def _stop_progress(self, stop: threading.Event, thread: threading.Thread | None) -> None:
        stop.set()
        if thread is not None and thread.ident is not None:
            thread.join()
        with self._lock:
            if self._active_line is not None:
                self._active_line = None
                self._rewrite_line("")
                self._line_width = 0

    def dataset_result(
        self, dataset: dict, result: dict | None = None, *, status: str = "PASS"
    ) -> None:
        self.dataset_scope(dataset)
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
            if result is not None and key in result:
                metrics.append(f"{result[key]:,} {unit}")
        label = _DATASET_NAMES.get(dataset["type"], dataset["type"])
        self.summary(f"  {label:<22} {status:<13} {' / '.join(metrics)}".rstrip())

    def verification_header(
        self, manifest: dict, *, cached: bool = False, installation_status: str | None = None
    ) -> None:
        self._scope = None
        if installation_status:
            self.summary(f"\nInstallation: {installation_status.upper()}")
        self.summary("Manifest:     " + ("UNCHANGED / PASS" if cached else "PASS (Ed25519, ETag)"))
        self.summary(f"Datasets:     {len(manifest['datasets'])}")

    def verification(
        self,
        manifest: dict,
        results: dict,
        *,
        cached: bool = False,
        installation_status: str | None = None,
    ) -> None:
        """Render an already verified snapshot, without starting an animation."""
        self.verification_header(manifest, cached=cached, installation_status=installation_status)
        for dataset in ordered_datasets(manifest):
            result = results.get(dataset["type"])
            self.dataset_result(
                dataset,
                result,
                status=("CACHED / PASS" if cached else "PASS")
                if result is not None
                else "SKIPPED (not verified)",
            )
        self.verification_footer(manifest, results, cached=cached)

    def verification_footer(self, manifest: dict, results: dict, *, cached: bool = False) -> None:
        """Only called after the manifest's conditional GET has succeeded."""
        stats = verification_statistics(manifest, results, cached=cached)
        totals = stats["cached_verified"] if cached else stats["downloaded_verified"]
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
