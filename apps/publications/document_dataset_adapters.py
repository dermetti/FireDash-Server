"""Dataset-specific inputs for generic document-manifest-v2 generation."""

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from uuid import UUID

from apps.publications.builders import PublicationBuildError
from apps.reference_data.models import FirePlan, KlgvPlan

if TYPE_CHECKING:
    from apps.file_library.publication import FileLibraryAdapter


@dataclass(frozen=True)
class FrozenDocument:
    canonical_document_id: UUID | str
    sanitized_pdf_sha256: str
    metadata: dict[str, object]
    accepted_document_key: str


@dataclass(frozen=True)
class DocumentDatasetAdapter:
    dataset_type_code: str
    snapshot_key: str
    manifest_metadata_key: str
    manifest_metadata_fields: tuple[str, ...]

    def frozen_documents(self, publication) -> tuple[FrozenDocument, ...]:
        if (
            publication.station_id
            or not publication.department_id
            or publication.scope_type != "DEPARTMENT"
            or publication.dataset_type_code != self.dataset_type_code
        ):
            raise PublicationBuildError("Document dataset requires a department scope.")
        snapshot = publication.source_snapshot
        entries = snapshot.get(self.snapshot_key) if isinstance(snapshot, dict) else None
        if not isinstance(entries, list):
            raise PublicationBuildError("Frozen document source is unavailable.")
        identities = [
            (entry.get("id"), entry.get("sha256")) for entry in entries if isinstance(entry, dict)
        ]
        if (
            len(identities) != len(entries)
            or any(
                not isinstance(document_id, str)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                for document_id, digest in identities
            )
            or len({document_id for document_id, _ in identities}) != len(identities)
        ):
            raise PublicationBuildError("Frozen document source is invalid.")
        try:
            if any(str(UUID(document_id)) != document_id for document_id, _ in identities):
                raise ValueError
        except (ValueError, TypeError) as error:
            raise PublicationBuildError("Frozen document identity is invalid.") from error
        validated_identities = cast(list[tuple[str, str]], identities)
        is_fire_plan = self.dataset_type_code == "department_fire_plans"
        model = FirePlan if is_fire_plan else KlgvPlan
        plans = {
            str(plan.id): plan
            for plan in model.objects.filter(
                department_id=publication.department_id,
                id__in=[document_id for document_id, _ in validated_identities],
            )
        }
        if len(plans) != len(identities) or any(
            plans[document_id].sha256 != digest for document_id, digest in validated_identities
        ):
            raise PublicationBuildError("Accepted document hash does not match frozen metadata.")
        metadata = {str(entry["id"]): entry for entry in entries}
        snapshot_fields = set(self.manifest_metadata_fields) | ({"path"} if is_fire_plan else set())
        if any(set(entry) != snapshot_fields for entry in metadata.values()):
            raise PublicationBuildError("Frozen document metadata is invalid.")
        if is_fire_plan:
            for document_id, entry in metadata.items():
                if entry["path"] != f"plans/{document_id}.pdf":
                    raise PublicationBuildError("Frozen document path is invalid.")
                if type(entry["page_count"]) is not int or entry["page_count"] <= 0:
                    raise PublicationBuildError("Frozen document page count is invalid.")
                if any(
                    entry[field] is not None and not isinstance(entry[field], str)
                    for field in self.manifest_metadata_fields
                    if field not in {"id", "sha256", "page_count", "longitude", "latitude"}
                ):
                    raise PublicationBuildError("Frozen document metadata is invalid.")
                for field, bound in (("longitude", 180), ("latitude", 90)):
                    value = entry[field]
                    if value is not None and (
                        type(value) not in (int, float)
                        or not math.isfinite(value)
                        or abs(value) > bound
                    ):
                        raise PublicationBuildError("Frozen document coordinate is invalid.")
                if (entry["longitude"] is None) != (entry["latitude"] is None):
                    raise PublicationBuildError("Frozen document coordinates are incomplete.")
        return tuple(
            FrozenDocument(
                canonical_document_id=plans[document_id].id,
                sanitized_pdf_sha256=digest,
                metadata={
                    field: metadata[document_id][field] for field in self.manifest_metadata_fields
                },
                accepted_document_key=(
                    plans[document_id].document_key if is_fire_plan else plans[document_id].path
                ),
            )
            for document_id, digest in validated_identities
        )


_KLGV_ADAPTER = DocumentDatasetAdapter(
    dataset_type_code="department_klgv_plans",
    snapshot_key="klgv_plans",
    manifest_metadata_key="klgv_plan",
    manifest_metadata_fields=(
        "id",
        "external_identifier",
        "object_name",
        "address",
        "postal_code",
        "city",
        "longitude",
        "latitude",
        "sha256",
        "page_count",
    ),
)


_FIRE_PLAN_ADAPTER = DocumentDatasetAdapter(
    dataset_type_code="department_fire_plans",
    snapshot_key="fire_plans",
    manifest_metadata_key="fire_plan",
    manifest_metadata_fields=(
        "id",
        "external_identifier",
        "object_name",
        "address",
        "postal_code",
        "city",
        "fsd_location",
        "bmz_location",
        "rwa_info",
        "longitude",
        "latitude",
        "sha256",
        "page_count",
    ),
)


def document_dataset_adapter(
    *, dataset_type_code: str
) -> "DocumentDatasetAdapter | FileLibraryAdapter":
    if dataset_type_code in {"department_files", "station_files"}:
        from apps.file_library.publication import FileLibraryAdapter

        return FileLibraryAdapter()
    if dataset_type_code == _FIRE_PLAN_ADAPTER.dataset_type_code:
        return _FIRE_PLAN_ADAPTER
    if dataset_type_code == _KLGV_ADAPTER.dataset_type_code:
        return _KLGV_ADAPTER
    raise PublicationBuildError("No document-manifest-v2 adapter is registered for this dataset.")
