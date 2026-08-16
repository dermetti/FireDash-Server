"""Client-side failure type for the fake iPad tool."""

from __future__ import annotations

from typing import NoReturn


class ClientError(RuntimeError):
    """A client-side, protocol, or transport failure.

    Raised for anything that should terminate the current command with a
    non-zero exit code and a safe, human-readable message. It deliberately
    never carries secret material in its message.
    """


def fail(message: str) -> NoReturn:
    raise ClientError(message)
