import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models.deletion import ProtectedError

from apps.audit.services import record_event
from apps.authorization.scopes import active_department_ids, active_station_ids, is_system_admin
from apps.file_library.models import Content, Entry, Library
from apps.organizations.models import Department, Station
from apps.reference_data import pdf_sandbox, storage
from apps.reference_data.pdf_validation import validate_pdf


class LibraryError(ValueError):
    pass


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


def locked_library(actor, library_id):
    from apps.file_library.publication import lock_scope

    preview = Library.objects.get(pk=library_id)
    require_library(actor, preview)
    lock_scope(preview)
    library = Library.objects.select_for_update().get(pk=library_id)
    require_library(actor, library)
    return library


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


@transaction.atomic
def rename_library(*, actor, library_id, name):
    library = locked_library(actor, library_id)
    name = name_value(name)
    if library.name != name:
        library.name = name
        changed(actor, library, library, "library_renamed")
    return library


@transaction.atomic
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


@transaction.atomic
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


@transaction.atomic
def delete_entry(*, actor, library_id, entry_id):
    library = locked_library(actor, library_id)
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
    )
    # Immutable content remains until explicit reference-aware collection.


def upload_pdf(*, actor, library_id, upload, parent_id=None, replace_id=None):
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
            if usage > library.quota_bytes:
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
