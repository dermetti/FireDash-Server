"""Authorized, bounded read models for the future File Explorer; no rendering."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from django.core.paginator import Page, Paginator
from django.db.models import F, QuerySet
from django.db.models.functions import Lower

from apps.accounts.models import User
from apps.file_library import services
from apps.file_library.models import Entry, Library

Sort = Literal["name", "modified", "size"]
MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class ExplorerItem:
    entry: Entry
    # Containing folders only; a folder result itself is not part of this path.
    ancestors: tuple[Entry, ...]


@dataclass(frozen=True)
class ExplorerReadModel:
    library: Library
    current_folder: Entry | None
    breadcrumbs: tuple[Entry, ...]
    page: Page[Entry]
    items: tuple[ExplorerItem, ...]
    file_count: int
    usage_bytes: int
    access_mode: str
    operations: frozenset[str]
    query: str
    sort: Sort
    descending: bool


def ordered_entries(
    entries: QuerySet[Entry], *, sort: Sort = "name", descending: bool = False
) -> QuerySet[Entry]:
    """Folder-first database ordering; UUID resolves ties in either direction."""
    if sort not in {"name", "modified", "size"}:
        raise services.LibraryError("Unknown file ordering.")
    if sort == "name":
        order = Lower("name").desc() if descending else Lower("name").asc()
    else:
        column = F("modified_at" if sort == "modified" else "content__size_bytes")
        order = column.desc(nulls_last=True) if descending else column.asc(nulls_last=True)
    return entries.order_by("-is_folder", order, "id")


def read_explorer(
    *,
    actor: User,
    library_id: UUID,
    folder_id: UUID | None = None,
    query: str = "",
    sort: Sort = "name",
    descending: bool = False,
    page: int = 1,
    page_size: int = 25,
) -> ExplorerReadModel:
    """Authorize from persisted state on every call; mutations still use services.

    Search covers the whole authorized library, including nested folders. Invalid
    folders/pages fail explicitly rather than silently navigating somewhere else.
    Only one page and its bounded (at most five levels) ancestry are materialized.
    """
    library = Library.objects.select_related("station__department", "department").get(pk=library_id)
    access = services.require_library_access(actor, library, permission="read")
    current = services.parent_entry(library, folder_id)
    breadcrumbs = services.folder_ancestry(library, current)
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= MAX_PAGE_SIZE
    ):
        raise services.LibraryError(f"Page size must be between 1 and {MAX_PAGE_SIZE}.")
    query = query.strip()[:255]
    entries = library.entries.select_related(
        "content", "modified_by", "parent__parent__parent__parent__parent"
    )
    entries = entries.filter(name__icontains=query) if query else entries.filter(parent=current)
    selected = Paginator(
        ordered_entries(entries, sort=sort, descending=descending), page_size
    ).page(page)
    items = tuple(
        ExplorerItem(entry, services.folder_ancestry(library, entry.parent)) for entry in selected
    )
    operations = frozenset({"preview", "download", "delete"})
    if access == services.ACCESS_NORMAL:
        operations |= {"upload", "create_folder", "rename", "move", "replace"}
    return ExplorerReadModel(
        library=library,
        current_folder=current,
        breadcrumbs=breadcrumbs,
        page=selected,
        items=items,
        file_count=library.entries.filter(is_folder=False).count(),
        usage_bytes=library.usage_bytes,
        access_mode=access,
        operations=operations,
        query=query,
        sort=sort,
        descending=descending,
    )
