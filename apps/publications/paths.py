"""Canonical publication artifact path helpers.

The final artifact layout is enforced by a PostgreSQL closeout trigger and must
be the single source of truth here so build, model validation, serving, and
tests never drift apart again.

Layout (forward-slash, database/API form):

    <department_id>/<publication_id>/artifact.bin (DEPARTMENT/STATION)
    system/<publication_id>/artifact.bin (SYSTEM)

The on-disk file lives under ``PUBLICATION_ARTIFACT_ROOT`` using this same
relative path.
"""

from __future__ import annotations

ARTIFACT_FILENAME = "artifact.bin"


def publication_artifact_relative_path(*, department_id: object | None, publication_id: object, scope_type: str = "DEPARTMENT") -> str:
    """Return the canonical forward-slash relative artifact path for a publication."""
    if scope_type == "SYSTEM":
        if department_id is not None:
            raise ValueError("SYSTEM publication artifacts cannot have a department owner.")
        return f"system/{publication_id}/{ARTIFACT_FILENAME}"
    if scope_type not in ("DEPARTMENT", "STATION") or department_id is None:
        raise ValueError("Tenant publication artifacts require a valid owner scope.")
    return f"{department_id}/{publication_id}/{ARTIFACT_FILENAME}"


def document_artifact_relative_path(*, artifact_id: object) -> str:
    """Return the identity-addressed path for an immutable Fire Plan PDF.

    This deliberately contains neither a publication version nor a source
    filename.  A document artifact remains addressable across future
    generations that reference it.
    """
    return f"documents/{artifact_id}/{ARTIFACT_FILENAME}"
