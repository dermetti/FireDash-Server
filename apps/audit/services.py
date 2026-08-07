import uuid

from django.http import HttpRequest

from apps.audit.models import AuditEvent
from apps.organizations.models import Department, Station


def record_event(
    *,
    action: str,
    request: HttpRequest | None = None,
    actor_user=None,
    department: Department | None = None,
    station: Station | None = None,
    target_type: str,
    target_uuid: uuid.UUID | None = None,
    metadata: dict[str, str | int | bool] | None = None,
) -> AuditEvent:
    request_id = getattr(request, "request_id", uuid.uuid4())
    return AuditEvent.objects.create(
        action=action,
        actor_user=actor_user,
        department=department,
        station=station,
        target_type=target_type,
        target_uuid=target_uuid,
        request_id=request_id,
        source_ip=getattr(request, "client_ip", None),
        user_agent=(request.META.get("HTTP_USER_AGENT", "")[:512] if request else ""),
        metadata=metadata or {},
    )
