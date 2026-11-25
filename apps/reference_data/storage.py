import hashlib
import os
import shutil
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType
from typing import Any, cast

try:  # pragma: no cover - Windows has no Unix group database.
    import grp as _grp

    grp: ModuleType | None = _grp
except ModuleNotFoundError:  # pragma: no cover - exercised through configured-group tests.
    grp = None

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile

from apps.reference_data import pdf_sandbox
from apps.reference_data.pdf_validation import PdfValidationError

_promotions: ContextVar[list[Path] | None] = ContextVar("pdf_promotions", default=None)


@contextmanager
def promotion_batch():
    """Clean new physical identities if the surrounding database operation fails.

    Use outside transaction.atomic so commit failures are covered as well.
    Never remove pre-existing accepted content or consume the preview on failure.
    """
    paths: list[Path] = []
    token = _promotions.set(paths)
    try:
        yield
    except BaseException:
        for path in paths:
            cleanup_unreferenced_accepted(path)
        raise
    finally:
        _promotions.reset(token)


def process_pdf(source: Path, output: Path, *, original_filename: str, strict: bool = False):
    """Shared preliminary checks; all native PDF/MIME inspection is isolated."""
    if not original_filename.casefold().endswith(".pdf"):
        raise PdfValidationError("Only PDF uploads are accepted.", code="unsupported_extension")
    if source.stat().st_size > settings.MAX_PDF_INPUT_BYTES:
        raise PdfValidationError(
            "PDF input exceeds the configured size limit.", code="input_too_large"
        )
    with source.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise PdfValidationError(
                "Upload does not have a PDF signature.", code="invalid_pdf_signature"
            )
    try:
        return pdf_sandbox.sanitize(
            quarantined_input=source, sanitized_output=output, strict=strict
        )
    except pdf_sandbox.PdfSanitizerContentError as error:
        raise PdfValidationError(str(error), code=error.code) from error


def promote_preview(source: Path, key: str, *, sha256: str) -> Path:
    """Keep the staged preview retryable, including after database rollback."""
    temporary = source.with_name(f"{uuid.uuid4()}.pdf")
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != sha256:
                raise StorageError("Sanitized preview integrity verification failed.")
        return promote_to_accepted(temporary, key)
    finally:
        cleanup(temporary)


def retire_plan_content(*, key: str, document_id, sha256: str) -> None:
    """Remove superseded source bytes only after commit and without retained users."""
    from django.db import transaction

    def collect():
        from apps.reference_data.models import FirePlan, KlgvPlan

        if (
            FirePlan.objects.filter(document_key=key).exists()
            or KlgvPlan.objects.filter(path=key).exists()
        ):
            return
        if _plan_source_retained(sha256=sha256, document_id=document_id):
            return
        relative = Path(key)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".pdf":
            return
        cleanup(settings.REFERENCE_DATA_ACCEPTED_ROOT / relative)

    transaction.on_commit(collect, robust=True)


def _plan_source_retained(*, sha256: str, document_id=None) -> bool:
    from django.db.models import Q

    from apps.publications.models import DatasetPublication

    identity = {"sha256": sha256}
    if document_id is not None:
        identity["id"] = str(document_id)
    return DatasetPublication.objects.filter(
        Q(source_snapshot__fire_plans__contains=[identity])
        | Q(source_snapshot__klgv_plans__contains=[identity]),
        dataset_type_code__in=("department_fire_plans", "department_klgv_plans"),
        source_snapshot__isnull=False,
    ).exists()


def cleanup_unreferenced_accepted(path: Path | None) -> None:
    """A post-commit callback failure must never delete committed document bytes.

    If the database cannot establish that content is unreferenced, retain it.
    """
    import logging

    from django.db import DatabaseError

    from apps.file_library.models import Content
    from apps.reference_data.models import FirePlan, KlgvPlan

    if path is None:
        return
    try:
        key = path.relative_to(settings.REFERENCE_DATA_ACCEPTED_ROOT).as_posix()
        if (
            FirePlan.objects.filter(document_key=key).exists()
            or KlgvPlan.objects.filter(path=key).exists()
            or Content.objects.filter(storage_key=key).exists()
        ):
            return
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if _plan_source_retained(sha256=digest):
            return
    except FileNotFoundError:
        return
    except (DatabaseError, OSError, ValueError):
        logging.getLogger(__name__).warning("Accepted PDF cleanup deferred; references uncertain.")
        return
    cleanup(path)


class StorageError(RuntimeError):
    pass


def write_quarantine(upload: UploadedFile, *, limit: int | None = None) -> Path:
    _ensure_private_roots()
    job_directory = settings.REFERENCE_DATA_QUARANTINE_ROOT / str(uuid.uuid4())
    # Owner-only creation: the unprivileged process must not request SUID/SGID bits,
    # which RestrictSUIDSGID prohibits. The setgid parent may still propagate group/SGID.
    job_directory.mkdir(mode=0o700)
    path = job_directory / "input.pdf"
    try:
        _write_limited(
            upload,
            path,
            min(limit, settings.MAX_PDF_INPUT_BYTES)
            if limit is not None
            else settings.MAX_PDF_INPUT_BYTES,
        )
        os.chmod(path, 0o640)
    except Exception:
        cleanup(path)
        raise
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


def promote_to_accepted(source: Path, document_key: str) -> Path:
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
    if destination.exists():
        raise StorageError("Generated document key already exists.")
    os.chmod(source, 0o640)
    # Exclusive creation prevents even a colliding generated identity from
    # overwriting accepted bytes. As before, promotion requires one filesystem.
    os.link(source, destination)
    paths = _promotions.get()
    if paths is not None:
        paths.append(destination)
    try:
        _apply_accepted_group(destination, accepted_group_id=accepted_group_id)
        source.unlink()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def cleanup(path: Path | None) -> None:
    if path is None:
        return
    try:
        if path.parent.parent in (
            settings.REFERENCE_DATA_QUARANTINE_ROOT,
            settings.REFERENCE_DATA_SANITIZER_OUTPUT_ROOT,
        ):
            # Job directories also contain the bounded request/result contract.
            shutil.rmtree(path.parent)
            return
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
        return cast(int, grp.getgrnam(group_name).gr_gid)
    except KeyError as error:
        raise StorageError("Accepted document reader group is unavailable.") from error


def _apply_accepted_group(path: Path, *, accepted_group_id: int | None) -> None:
    """Apply the dedicated reader group after cross-directory promotion.

    Hard-link promotion preserves the source file group, so setgid inheritance of
    the accepted directory alone is insufficient for sanitized output moved
    into it. The backend owner may change its own file to this supplementary
    group; publication receives read-only access through mode 0640.
    """
    if accepted_group_id is None:
        return
    cast(Any, os).chown(path, -1, accepted_group_id)
    os.chmod(path, 0o640)
