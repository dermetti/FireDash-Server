"""Shared tablet configuration serialization for the API and signed manifests."""

from django.conf import settings

from apps.report_transfer.contract import configuration as report_transfer_configuration
from apps.tablets.models import AppInstallation


def tablet_configuration(*, installation: AppInstallation, vehicle) -> dict[str, object]:
    """Serialize an already authorized control-plane context."""
    return {
        "installation_id": str(installation.id),
        "tablet_id": str(installation.tablet_id),
        "department_id": str(installation.tablet.department_id),
        "station_id": str(vehicle.station_id) if vehicle is not None else None,
        "vehicle_id": str(vehicle.id) if vehicle is not None else None,
        "report_delivery_max_attachment_bytes": settings.OUTBOUND_MAIL_REPORT_ATTACHMENT_MAX_BYTES,
        "report_transfer": report_transfer_configuration(),
    }
