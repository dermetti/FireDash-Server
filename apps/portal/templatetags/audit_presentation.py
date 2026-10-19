import uuid

from django import template

register = template.Library()
REMOVED = "authorization.administrator_permanently_removed"


@register.filter
def audit_event_label(event):
    return "Administrator permanently removed" if event.action == REMOVED else event.action


@register.filter
def audit_event_context(event):
    """Only explicit, typed allowlisted values may cross the presentation boundary."""
    if event.action != REMOVED:
        return []
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    rows = [("Outcome", "Authorization was revoked and the user account anonymized.")]
    for field, label in (
        ("department_authority_count", "Department authority records"),
        ("station_authority_count", "Station authority records"),
    ):
        value = metadata.get(field)
        if type(value) is int and 0 <= value <= 1_000_000:
            rows.append((label, str(value)))
    department_count = metadata.get("department_authority_count")
    station_count = metadata.get("station_authority_count")
    if type(department_count) is int and type(station_count) is int:
        if department_count > 0 and station_count > 0:
            rows.append(("Scope type", "Department and station"))
        elif department_count > 0:
            rows.append(("Scope type", "Department"))
        elif station_count > 0:
            rows.append(("Scope type", "Station"))
    station_id = metadata.get("station_id")
    if isinstance(station_id, str):
        try:
            rows.append(("Station authority scope UUID", str(uuid.UUID(station_id))))
        except ValueError:
            pass
    return rows
