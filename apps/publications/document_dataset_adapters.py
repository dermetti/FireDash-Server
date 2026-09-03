"""Dataset-specific inputs for generic document-manifest-v2 generation."""

from dataclasses import dataclass

from apps.publications.builders import PublicationBuildError
from apps.reference_data.models import KlgvPlan


@dataclass(frozen=True)
class FrozenDocument:
    canonical_document_id: object
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
        if publication.station_id:
            raise PublicationBuildError("Document dataset requires a department scope.")
        snapshot = publication.source_snapshot
        entries = snapshot.get(self.snapshot_key) if isinstance(snapshot, dict) else None
        if not isinstance(entries, list):
            raise PublicationBuildError("Frozen document source is unavailable.")
        identities = [
            (entry.get("id"), entry.get("sha256"))
            for entry in entries
            if isinstance(entry, dict)
        ]
        if len(identities) != len(entries) or any(
            not isinstance(document_id, str) or not isinstance(digest, str) or len(digest) != 64
            for document_id, digest in identities
        ) or len({document_id for document_id, _ in identities}) != len(identities):
            raise PublicationBuildError("Frozen document source is invalid.")
        plans = {
            str(plan.id): plan
            for plan in KlgvPlan.objects.filter(
                department_id=publication.department_id,
                id__in=[document_id for document_id, _ in identities],
            )
        }
        if len(plans) != len(identities) or any(
            plans[document_id].sha256 != digest for document_id, digest in identities
        ):
            raise PublicationBuildError(
                "Accepted KLGV document hash does not match frozen metadata."
            )
        metadata = {str(entry["id"]): entry for entry in entries}
        if any(set(entry) != set(self.manifest_metadata_fields) for entry in metadata.values()):
            raise PublicationBuildError("Frozen document metadata is invalid.")
        return tuple(
            FrozenDocument(
                canonical_document_id=plans[document_id].id,
                sanitized_pdf_sha256=digest,
                metadata={
                    field: metadata[document_id][field]
                    for field in self.manifest_metadata_fields
                },
                accepted_document_key=plans[document_id].path,
            )
            for document_id, digest in identities
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


def document_dataset_adapter(*, dataset_type_code: str) -> DocumentDatasetAdapter:
    if dataset_type_code == _KLGV_ADAPTER.dataset_type_code:
        return _KLGV_ADAPTER
    raise PublicationBuildError("No document-manifest-v2 adapter is registered for this dataset.")
