"""Locked, repeatable repair of file scopes left dirty by historical fingerprints."""

from django.db import transaction
from django.db.models import Q

from apps.file_library.locking import FILE_DATASETS
from apps.file_library.models import Library
from apps.file_library.publication import snapshot_for_library
from apps.publications.builders import source_fingerprint_for_payload
from apps.publications.models import DatasetScopeState, PublicationJob
from apps.publications.services import _locked_scope, _reconcile_clean_source, _scope_is_dirty


@transaction.atomic
def reconcile_file_scope(*, scope_id, department_id, apply=False):
    preview = DatasetScopeState.objects.get(
        pk=scope_id, department_id=department_id, dataset_type_code__in=FILE_DATASETS
    )
    scope = _locked_scope(
        department=preview.department,
        station=preview.station,
        dataset_type_code=preview.dataset_type_code,
    )
    library = Library.objects.filter(
        Q(station_id=scope.station_id) if scope.station_id else Q(department_id=scope.department_id)
    ).first()
    if library is None:
        return "no library"
    if PublicationJob.objects.filter(scope_state=scope, status="RUNNING").exists():
        return "building; deferred"
    snapshot = snapshot_for_library(library)
    digest = source_fingerprint_for_payload(snapshot)
    if _scope_is_dirty(scope=scope, fingerprint=digest):
        return "changed or historical snapshot unavailable; preserved"
    if apply:
        _reconcile_clean_source(scope=scope, snapshot=snapshot, fingerprint=digest)
    if PublicationJob.objects.filter(
        scope_state=scope, status="PENDING", allow_clean_rebuild=True
    ).exists():
        return "matches active publication; explicit force rebuild preserved"
    return (
        "matches active publication; reconciled"
        if apply
        else "matches active publication; eligible"
    )
