import uuid
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from apps.audit.services import record_event
from apps.authorization.scopes import active_department_ids, active_station_ids, is_system_admin
from apps.file_library.models import Content, Entry, Library, TemporaryStationAccess
from apps.organizations.models import Department, Station
from apps.reference_data import pdf_sandbox, storage
from apps.reference_data.pdf_validation import validate_pdf


class LibraryError(ValueError):
    pass


TEMPORARY_ACCESS_DURATION = timedelta(hours=6)
ACCESS_NORMAL = "normal"
ACCESS_TEMPORARY = "temporary"


def _library_mutation(function):
    """Persist expiry cleanup before entering a mutation's rollback boundary."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        active_temporary_access(kwargs["actor"])
        try:
            with transaction.atomic():
                return function(*args, **kwargs)
        except PermissionDenied:
            # The grant may expire between preflight and locked authorization.
            # Repeat cleanup after rollback so that revocation is durable too.
            active_temporary_access(kwargs["actor"])
            raise

    return wrapped


def require_scope(actor, *, department_id=None, station_id=None):
    if is_system_admin(actor):
        raise PermissionDenied
    departments = active_department_ids(actor)
    if department_id is not None and station_id is None and department_id in departments:
        return
    if (
        station_id is not None
        and department_id is None
        and not departments.exists()
        and station_id in active_station_ids(actor)
    ):
        return
    raise PermissionDenied


def require_library(actor, library):
    require_scope(actor, department_id=library.department_id, station_id=library.station_id)


def _revoke_access(
    access,
    *,
    reason,
    actor=None,
    target_uuid=None,
    target_type="temporary_station_access",
    audit=True,
):
    if access.revoked_at is not None:
        return access
    now = timezone.now()
    access.revoked_at = now
    access.revocation_reason = reason
    access.save(update_fields=("revoked_at", "revocation_reason"))
    if not audit:
        return access
    record_event(
        action=f"file_library.temporary_access_{reason}",
        actor_user=actor or access.user,
        department=access.department,
        station=access.station,
        target_type=target_type,
        target_uuid=target_uuid or access.id,
        metadata={
            "access_id": str(access.id),
            "access_user_id": str(access.user_id),
            "activated_at": access.activated_at.isoformat(),
            "expires_at": access.expires_at.isoformat(),
            "revoked_at": now.isoformat(),
        },
    )
    return access


@transaction.atomic
def active_temporary_access(actor, *, lock=False):
    query = TemporaryStationAccess.objects.select_related("department", "station")
    if lock:
        query = query.select_for_update()
    access = query.filter(user=actor, revoked_at__isnull=True).first()
    if access is None:
        return None
    now = timezone.now()
    if access.expires_at <= now:
        _revoke_access(access, reason="expired")
        return None
    if (
        access.department_id not in active_department_ids(actor)
        or not access.station.active
        or access.station.department_id != access.department_id
    ):
        _revoke_access(access, reason="authority_revoked")
        return None
    return access


@transaction.atomic
def activate_temporary_access(*, actor, station):
    if station.department_id not in active_department_ids(actor) or not station.active:
        raise PermissionDenied
    # Serialize activation/switching for one user. The partial unique constraint is
    # the final defense against concurrent requests.
    get_user_model().objects.select_for_update().get(pk=actor.pk)
    previous = active_temporary_access(actor, lock=True)
    if previous and previous.station_id == station.id:
        return previous
    switched_from = previous.station_id if previous else None
    switched_access_id = previous.id if previous else None
    if previous:
        _revoke_access(previous, reason="switched", actor=actor, audit=False)
    now = timezone.now()
    access = TemporaryStationAccess.objects.create(
        user=actor,
        department=station.department,
        station=station,
        activated_at=now,
        expires_at=now + TEMPORARY_ACCESS_DURATION,
    )
    record_event(
        action=(
            "file_library.temporary_access_switched"
            if switched_from
            else "file_library.temporary_access_activated"
        ),
        actor_user=actor,
        department=station.department,
        station=station,
        target_type="temporary_station_access",
        target_uuid=access.id,
        metadata={
            "station_id": str(station.id),
            "previous_station_id": str(switched_from) if switched_from else None,
            "previous_access_id": str(switched_access_id) if switched_access_id else None,
            "activated_at": now.isoformat(),
            "expires_at": access.expires_at.isoformat(),
        },
    )
    library, _ = Library.objects.get_or_create(station=station)
    from apps.file_library.publication import initialize_library

    initialize_library(library)
    return access


@transaction.atomic
def revoke_temporary_access(
    *, actor, reason="released", target_uuid=None, target_type="temporary_station_access"
):
    access = active_temporary_access(actor, lock=True)
    if access:
        _revoke_access(
            access,
            reason=reason,
            actor=actor,
            target_uuid=target_uuid,
            target_type=target_type,
        )
    return access


@transaction.atomic
def revoke_temporary_access_for_user(*, user, actor, reason="authority_revoked"):
    access = (
        TemporaryStationAccess.objects.select_for_update()
        .select_related("department", "station")
        .filter(user=user, revoked_at__isnull=True)
        .first()
    )
    if access:
        _revoke_access(access, reason=reason, actor=actor)
    return access


@transaction.atomic
def revoke_department_temporary_accesses(*, department, actor, reason="authority_revoked"):
    accesses = list(
        TemporaryStationAccess.objects.select_for_update()
        .select_related("department", "station", "user")
        .filter(department=department, revoked_at__isnull=True)
        .order_by("id")
    )
    for access in accesses:
        _revoke_access(access, reason=reason, actor=actor)
    return len(accesses)


def require_library_access(actor, library, *, permission):
    try:
        require_library(actor, library)
        return ACCESS_NORMAL
    except PermissionDenied:
        if permission not in {"read", "delete"} or not library.station_id:
            raise
    access = active_temporary_access(actor, lock=permission == "delete")
    if access is None or access.station_id != library.station_id:
        raise PermissionDenied
    return ACCESS_TEMPORARY


@transaction.atomic
def get_library(actor, *, department_id=None, station_id=None):
    require_scope(actor, department_id=department_id, station_id=station_id)
    scope = {"department_id": department_id} if department_id else {"station_id": station_id}
    library, _ = Library.objects.get_or_create(**scope)
    from apps.file_library.publication import initialize_library

    initialize_library(library)
    return library


def authorized_libraries(actor):
    if is_system_admin(actor):
        return []
    departments = Department.objects.filter(id__in=active_department_ids(actor))
    if departments.exists():
        return [get_library(actor, department_id=item.id) for item in departments]
    return [
        get_library(actor, station_id=item.id)
        for item in Station.objects.filter(id__in=active_station_ids(actor))
    ]


def locked_library(actor, library_id, *, permission="manage", return_access=False):
    from apps.file_library.publication import lock_scope

    preview = Library.objects.get(pk=library_id)
    access = require_library_access(actor, preview, permission=permission)
    lock_scope(preview)
    library = Library.objects.select_for_update().get(pk=library_id)
    access = require_library_access(actor, library, permission=permission)
    return (library, access) if return_access else library


def name_value(value, *, pdf=False):
    value = value.strip()
    if (
        not value
        or len(value) > 255
        or value in {".", ".."}
        or any(ord(c) < 32 or c in "/\\" for c in value)
    ):
        raise LibraryError("Enter a name of 1–255 characters without path separators.")
    if pdf and not value.lower().endswith(".pdf"):
        raise LibraryError("Document names must end in .pdf.")
    return value


def scoped_entry(library, entry_id, *, folder=False):
    try:
        entry = Entry.objects.get(library=library, pk=entry_id)
    except (Entry.DoesNotExist, ValueError) as error:
        raise LibraryError("The selected item is unavailable in this library.") from error
    if folder and not entry.is_folder:
        raise LibraryError("Select a folder.")
    return entry


def parent_entry(library, parent_id):
    return scoped_entry(library, parent_id, folder=True) if parent_id else None


def validate_location(library, name, parent, *, entry=None, folder=False):
    duplicates = Entry.objects.filter(library=library, parent=parent, name__iexact=name)
    if entry:
        duplicates = duplicates.exclude(pk=entry.pk)
    if duplicates.exists():
        raise LibraryError("An item with this name already exists in the folder.")
    depth = 0
    cursor = parent
    while cursor:
        depth += 1
        if depth > 5 or (entry and cursor.pk == entry.pk):
            raise LibraryError("Folder cycles are not allowed.")
        cursor = cursor.parent
    height = 1 if folder else 0
    if entry and folder:
        frontier = [entry.id]
        while frontier:
            frontier = list(
                Entry.objects.filter(parent_id__in=frontier, is_folder=True).values_list(
                    "id", flat=True
                )
            )
            if frontier:
                height += 1
    if depth + height > 5:
        raise LibraryError("Folders may be nested at most five levels deep.")


def changed(actor, library, target, operation, **metadata):
    library.source_revision += 1
    library.save(update_fields=("source_revision", "usage_bytes", "name"))
    record_event(
        action=f"file_library.{operation}",
        actor_user=actor,
        department=library.department if library.department_id else library.station.department,
        station=library.station,
        target_type="library" if target == library else "library_entry",
        target_uuid=target.id,
        metadata={
            "library_id": str(library.id),
            "source_revision": library.source_revision,
            **metadata,
        },
    )
    from apps.file_library.publication import scope_kwargs
    from apps.publications.services import mark_dirty

    kwargs = scope_kwargs(library)
    kwargs.pop("scope_type")
    mark_dirty(**kwargs, actor=actor)
    if library.department_id:
        revoke_temporary_access(actor=actor, reason="department_mutation")


@_library_mutation
def rename_library(*, actor, library_id, name):
    library = locked_library(actor, library_id)
    name = name_value(name)
    if library.name != name:
        library.name = name
        changed(actor, library, library, "library_renamed")
    return library


@_library_mutation
def create_folder(*, actor, library_id, name, parent_id=None):
    library = locked_library(actor, library_id)
    name = name_value(name)
    parent = parent_entry(library, parent_id)
    validate_location(library, name, parent, folder=True)
    entry = Entry.objects.create(
        library=library, name=name, parent=parent, is_folder=True, modified_by=actor
    )
    changed(actor, library, entry, "folder_created")
    return entry


@_library_mutation
def edit_entry(*, actor, library_id, entry_id, name=None, parent_id=None, move=False):
    library = locked_library(actor, library_id)
    entry = scoped_entry(library, entry_id)
    new_name = name_value(name, pdf=not entry.is_folder) if name is not None else entry.name
    parent = parent_entry(library, parent_id) if move else entry.parent
    validate_location(library, new_name, parent, entry=entry, folder=entry.is_folder)
    if new_name == entry.name and parent == entry.parent:
        return entry
    entry.name, entry.parent, entry.modified_by = new_name, parent, actor
    entry.save()
    changed(
        actor,
        library,
        entry,
        "moved" if move else "renamed",
        is_folder=entry.is_folder,
        parent_id=str(parent.id) if parent else None,
    )
    return entry


@_library_mutation
def delete_entry(*, actor, library_id, entry_id):
    library, access = locked_library(
        actor, library_id, permission="delete", return_access=True
    )
    entry = scoped_entry(library, entry_id)
    ids, frontier = [entry.id], [entry.id]
    while frontier:
        frontier = list(Entry.objects.filter(parent_id__in=frontier).values_list("id", flat=True))
        ids.extend(frontier)
    items = list(Entry.objects.filter(pk__in=ids).select_related("content"))
    library.usage_bytes -= sum(item.content.size_bytes for item in items if item.content_id)
    # Snapshot the complete post-deletion source, preserving the audit identity.
    target_id = entry.id
    entry.delete()
    entry.id = target_id
    changed(
        actor,
        library,
        entry,
        "deleted",
        is_folder=entry.is_folder,
        item_count=len(items),
        removed_ids=",".join(str(item.id) for item in items),
        elevated=access == ACCESS_TEMPORARY,
    )
    if access == ACCESS_TEMPORARY:
        revoke_temporary_access(
            actor=actor,
            reason="consumed_by_deletion",
            target_uuid=target_id,
            target_type="library_entry",
        )
    # Immutable content remains until explicit reference-aware collection.
    return access


def upload_pdf(*, actor, library_id, upload, parent_id=None, replace_id=None):
    # Expiry revocation must commit even when the following authorization fails.
    active_temporary_access(actor)
    library = Library.objects.get(pk=library_id)
    require_library(actor, library)
    name = name_value(upload.name, pdf=True)
    if upload.size > settings.FILE_LIBRARY_MAX_FILE_BYTES:
        raise LibraryError("PDF exceeds the file size limit.")
    source = output = accepted = None
    content_id = uuid.uuid4()
    key = f"file-libraries/{library.id}/{content_id}.pdf"
    try:
        source = storage.write_quarantine(upload, limit=settings.FILE_LIBRARY_MAX_FILE_BYTES)
        if source.stat().st_size > settings.FILE_LIBRARY_MAX_FILE_BYTES:
            raise LibraryError("PDF exceeds the file size limit.")
        validate_pdf(source, original_filename=name, strict=True)
        output = storage.output_path(job_id=source.parent.name)
        pdf_sandbox.sanitize(quarantined_input=source, sanitized_output=output)
        size, _pages, digest = validate_pdf(output, strict=True)
        if size > settings.FILE_LIBRARY_MAX_FILE_BYTES:
            raise LibraryError("Sanitized PDF exceeds the file size limit.")
        with transaction.atomic():
            from apps.file_library.reconciliation import lock_storage

            library = locked_library(actor, library_id)
            lock_storage(library_id)
            entry = scoped_entry(library, replace_id) if replace_id else None
            if entry and entry.is_folder:
                raise LibraryError("Only PDFs can be replaced.")
            parent = entry.parent if entry else parent_entry(library, parent_id)
            validate_location(library, entry.name if entry else name, parent, entry=entry)
            old_size = entry.content.size_bytes if entry else 0
            usage = library.usage_bytes - old_size + size
            if usage > library.quota_bytes and usage > library.usage_bytes:
                raise LibraryError("Library storage quota exceeded.")
            # Set the intended path before promotion so partial promotion failures are cleaned.
            destination = settings.REFERENCE_DATA_ACCEPTED_ROOT / key
            if destination.exists():
                raise LibraryError("Content identity already exists; retry the upload.")
            accepted = destination
            storage.promote_to_accepted(output, key)
            content = Content.objects.create(
                id=content_id, library=library, storage_key=key, size_bytes=size, sha256=digest
            )
            if entry:
                entry.content, entry.modified_by = content, actor
                entry.save()
            else:
                entry = Entry.objects.create(
                    library=library, parent=parent, name=name, content=content, modified_by=actor
                )
            library.usage_bytes = usage
            changed(
                actor,
                library,
                entry,
                "replaced" if replace_id else "uploaded",
                content_id=str(content.id),
            )
        accepted = None
        return entry
    finally:
        storage.cleanup(source)
        storage.cleanup(output)
        storage.cleanup(accepted)


def set_library_quota(*, actor, library_id, quota_bytes):
    """Set tenant policy without changing source revision or publication state."""
    active_temporary_access(actor)
    if (
        isinstance(quota_bytes, bool)
        or not isinstance(quota_bytes, int)
        or quota_bytes < 0
        or quota_bytes > settings.FILE_LIBRARY_MAX_QUOTA_BYTES
    ):
        raise LibraryError(
            f"Quota must be between 0 and {settings.FILE_LIBRARY_MAX_QUOTA_BYTES} bytes."
        )
    with transaction.atomic():
        # Lock only the library row. PostgreSQL rejects FOR UPDATE across the
        # nullable side of the department/station scope joins.
        library = Library.objects.select_for_update().get(pk=library_id)
        department = library.department or library.station.department
        from apps.authorization.services import require_department_admin

        require_department_admin(actor, department)
        old_value = library.quota_bytes
        if old_value == quota_bytes:
            return library
        library.quota_bytes = quota_bytes
        library.save(update_fields=("quota_bytes",))
        record_event(
            action="file_library.quota_changed",
            actor_user=actor,
            department=department,
            station=library.station,
            target_type="library",
            target_uuid=library.id,
            metadata={
                "library_id": str(library.id),
                "old_quota_bytes": old_value,
                "new_quota_bytes": quota_bytes,
                "usage_bytes": library.usage_bytes,
            },
        )
        revoke_temporary_access(actor=actor, reason="department_mutation")
        return library


def department_quota_libraries(*, actor, department):
    from apps.authorization.services import require_department_admin

    require_department_admin(actor, department)
    department_library, _ = Library.objects.get_or_create(department=department)
    station_libraries = []
    for station in department.stations.order_by("name", "id"):
        library, _ = Library.objects.get_or_create(station=station)
        station_libraries.append(library)
    return department_library, station_libraries


def content_path(content):
    root = settings.REFERENCE_DATA_ACCEPTED_ROOT.resolve()
    path = (root / content.storage_key).resolve()
    if not path.is_relative_to(root) or path.suffix != ".pdf":
        raise LibraryError("Content is unavailable.")
    return path


@transaction.atomic
def collect_content(content_id):
    """Maintenance-only GC. Future retained references must use PROTECT foreign keys.

    The DB FK also serializes concurrent new references against deletion. A failed
    physical unlink leaves an orphan, never a broken retained content reference.
    """
    content = Content.objects.select_for_update().get(pk=content_id)
    path = content_path(content)
    try:
        with transaction.atomic():
            content.delete()
    except ProtectedError:
        return False
    transaction.on_commit(lambda: storage.cleanup(path))
    return True
