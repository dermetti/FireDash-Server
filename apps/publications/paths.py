"""Canonical publication artifact path helpers.

The final artifact layout is enforced by a PostgreSQL closeout trigger and must
be the single source of truth here so build, model validation, serving, and
tests never drift apart again.

Layout (forward-slash, database/API form):

    <scope_type>/<department_id-or-system>/<publication_id>/artifact.bin

The on-disk file lives under ``PUBLICATION_ARTIFACT_ROOT`` using this same
relative path.
"""

from __future__ import annotations

ARTIFACT_FILENAME = "artifact.bin"


def publication_artifact_relative_path(*, department_id: object | None, publication_id: object, scope_type: str = "DEPARTMENT") -> str:
    """Return the canonical forward-slash relative artifact path for a publication."""
    owner = str(department_id) if department_id is not None else "system"
    return f"{scope_type.lower()}/{owner}/{publication_id}/{ARTIFACT_FILENAME}"


def document_artifact_relative_path(*, artifact_id: object) -> str:
    """Return the identity-addressed path for an immutable Fire Plan PDF.

    This deliberately contains neither a publication version nor a source
    filename.  A document artifact remains addressable across future
    generations that reference it.
    """
    return f"documents/{artifact_id}/{ARTIFACT_FILENAME}"
