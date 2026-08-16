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
    "hpke_private_key",
    "private_key",
}


def redact(value: Any, key: str | None = None) -> Any:
    """Recursively redact secrets while keeping useful shape/length info."""
    if key in _SECRET_KEYS and isinstance(value, str):
        return f"<redacted:{len(value)} chars>"
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

    def __init__(self, *, json_mode: bool = False, stream: TextIO | None = None) -> None:
        self.json_mode = json_mode
        self._stream = stream or sys.stdout

    def line(self, message: str = "") -> None:
        target = sys.stderr if self.json_mode else self._stream
        print(message, file=target)

    def banner(self, title: str) -> None:
        self.line("\n" + "=" * 72)
        self.line(title)
        self.line("=" * 72)

    def emit(self, result: dict[str, Any]) -> None:
        if not self.json_mode:
            return
        print(json.dumps(redact(result), sort_keys=True, ensure_ascii=False), file=self._stream)
