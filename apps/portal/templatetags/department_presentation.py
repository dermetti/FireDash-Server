from django import template

from apps.organizations.presentation import format_department_datetime

register = template.Library()


@register.filter
def department_datetime(value, department) -> str:
    """Department-local HTML presentation; never used for protocol timestamps."""
    return format_department_datetime(value, department)
