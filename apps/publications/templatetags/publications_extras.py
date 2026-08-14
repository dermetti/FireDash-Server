"""Template helpers for publication status rendering."""

from django import template

from apps.publications.registry import DATASET_REGISTRY

register = template.Library()

_STATUS_LABELS = {
    "BUILDING": "Building",
    "READY_FOR_REVIEW": "Ready to publish",
    "PUBLISHED": "Published",
    "FAILED": "Failed",
    "SUPERSEDED": "Superseded",
    "REJECTED": "Rejected",
    "OBSOLETE": "Out of date",
}

_STATUS_BADGES = {
    "BUILDING": "info",
    "READY_FOR_REVIEW": "warning",
    "PUBLISHED": "success",
    "FAILED": "danger",
    "SUPERSEDED": "secondary",
    "REJECTED": "secondary",
    "OBSOLETE": "secondary",
}


@register.filter
def publication_status_label(status):
    return _STATUS_LABELS.get(status, status)


@register.filter
def publication_status_badge(status):
    return _STATUS_BADGES.get(status, "secondary")


@register.filter
def dataset_display_name(code):
    definition = DATASET_REGISTRY.get(code)
    return definition.display_name if definition else code
