import os
import shutil
import uuid
from pathlib import Path

try:  # pragma: no cover - Windows has no Unix group database.
    import grp
except ModuleNotFoundError:  # pragma: no cover - exercised through configured-group tests.
    grp = None

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile


class StorageError(RuntimeError):
    pass


def write_quarantine(upload: UploadedFile) -> Path:
    _ensure_private_roots()
    job_directory = settings.REFERENCE_DATA_QUARANTINE_ROOT / str(uuid.uuid4())
    # Owner-only creation: the unprivileged process must not request SUID/SGID bits,
    # which RestrictSUIDSGID prohibits. The setgid parent may still propagate group/SGID.
    job_directory.mkdir(mode=0o700)
    path = job_directory / "input.pdf"
    _write_limited(upload, path, settings.MAX_PDF_INPUT_BYTES)
    os.chmod(path, 0o640)
    return path


def output_path(*, job_id: str | None = None) -> Path:
    _ensure_private_roots()
    job_directory = settings.REFERENCE_DATA_SANITIZER_OUTPUT_ROOT / (job_id or str(uuid.uuid4()))
    # Owner-only creation: the unprivileged process must not request SUID/SGID bits.
    # The setgid parent propagates the fire_pdf_sanitizer group to this directory, so a
    # plain non-SGID chmod below grants the sanitizer narrow write/traverse (no read/list).
    job_directory.mkdir(mode=0o700)
    os.chmod(job_directory, 0o730)  # nosec B103 - narrow sanitizer group write/traverse, no SUID/SGID
    return job_directory / "sanitized.pdf"


def promote_to_accepted(source: Path, document_key: str, *, replace: bool = False) -> Path:
    relative = Path(document_key)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix.lower() != ".pdf"
        or any(part in {"", "."} for part in relative.parts)
    ):
        raise StorageError("Invalid generated document key.")
    _ensure_private_roots()
    accepted_group_id = _accepted_group_id()
    destination = settings.REFERENCE_DATA_ACCEPTED_ROOT / relative
    # The deployed accepted root is setgid and group-readable by the dedicated
    # publication-source reader group. New nested directories must retain group
    # traverse/read permission; the parent supplies their group and SGID bit.
    destination.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if destination.exists() and not replace:
        raise StorageError("Generated document key already exists.")
    os.chmod(source, 0o640)
    os.replace(source, destination)
    _apply_accepted_group(destination, accepted_group_id=accepted_group_id)
    return destination


def cleanup(path: Path | None) -> None:
    if path is None:
        return
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        parent = path.parent
        if parent != settings.REFERENCE_DATA_SANITIZER_OUTPUT_ROOT:
            parent.rmdir()
    except OSError:
        pass


def _write_limited(upload: UploadedFile, path: Path, limit: int) -> None:
    size = 0
    with path.open("xb") as destination:
        for chunk in upload.chunks():
            size += len(chunk)
            if size > limit:
                destination.close()
                path.unlink(missing_ok=True)
                raise StorageError("PDF input exceeds the configured size limit.")
            destination.write(chunk)


def _ensure_private_roots() -> None:
    for root in (
        settings.REFERENCE_DATA_QUARANTINE_ROOT,
        settings.REFERENCE_DATA_SANITIZER_OUTPUT_ROOT,
        settings.REFERENCE_DATA_ACCEPTED_ROOT,
    ):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)


def _accepted_group_id() -> int | None:
    group_name = settings.REFERENCE_DATA_ACCEPTED_GROUP
    if not group_name:
        return None
    if grp is None:
        raise StorageError("Accepted document reader group is unavailable.")
    try:
        return grp.getgrnam(group_name).gr_gid
    except KeyError as error:
        raise StorageError("Accepted document reader group is unavailable.") from error


def _apply_accepted_group(path: Path, *, accepted_group_id: int | None) -> None:
    """Apply the dedicated reader group after cross-directory promotion.

    ``os.replace`` preserves the source file group, so setgid inheritance of
    the accepted directory alone is insufficient for sanitized output moved
    into it. The backend owner may change its own file to this supplementary
    group; publication receives read-only access through mode 0640.
    """
    if accepted_group_id is None:
        return
    os.chown(path, -1, accepted_group_id)
    os.chmod(path, 0o640)
