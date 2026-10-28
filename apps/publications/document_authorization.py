"""Shared authorization boundary for document generation delivery."""

from apps.publications.manifests import ManifestError


def _authorized_generation(*, publication, installation):
    """Authorize persisted generations against current tablet scope and entitlements."""
    from apps.authorization.services import minimum_supported_app_version
    from apps.publications.manifests import authorized_publications
    from apps.publications.models import DatasetPublication
    from apps.tablets.versions import AppVersionError, parse_app_version

    publication = DatasetPublication.objects.select_related("scope_state").get(pk=publication.pk)

    active_installation, vehicle, publications = authorized_publications(installation=installation)
    try:
        minimum = minimum_supported_app_version(api_major=1)
        version = parse_app_version(active_installation.app_version)
        if minimum is not None and version < minimum:
            raise ManifestError("Installation app version is no longer supported.")
    except AppVersionError as error:
        raise ManifestError("Installation app version is invalid.") from error
    if publication.schema_version != 2 or publication.dataset_type_code not in {
        "department_fire_plans",
        "department_klgv_plans",
        "department_files",
        "station_files",
    }:
        raise ManifestError("Publication is not an eligible document generation.")
    if publication.department_id != active_installation.tablet.department_id or vehicle is None:
        raise ManifestError("Installation is not authorized for this publication.")
    if publication.station_id and publication.station_id != vehicle.station_id:
        raise ManifestError("Installation is not authorized for this station.")
    # ``authorized_publications`` is the delivery boundary: it filters both
    # scope and the department feature gate. A guessed document-generation URL
    # must not bypass an otherwise hidden disabled dataset.
    if (
        publication.pk not in {candidate.pk for candidate in publications}
        or publication.scope_state.current_published_publication_id != publication.pk
    ):
        retained_library = (
            publication.dataset_type_code in {"department_files", "station_files"}
            and publication.status == publication.Status.SUPERSEDED
            and publication.artifact_ready
            and any(
                candidate.scope_state_id == publication.scope_state_id
                and candidate.pk == publication.scope_state.current_published_publication_id
                for candidate in publications
            )
        )
        if not retained_library:
            raise ManifestError("Installation is not authorized for this publication.")
    return active_installation
