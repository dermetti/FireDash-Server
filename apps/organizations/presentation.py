"""Department-scoped, administrator-facing date and time presentation.

Database, protocol, signing, and security timestamps deliberately remain UTC.
This module is only for HTML presentation where a Department context is known.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from django.utils import timezone as django_timezone

DEPARTMENT_TIMEZONE_CHOICES = [
    ("Europe/Berlin", "Europe/Berlin"),
    ("Europe/Vienna", "Europe/Vienna"),
    ("Europe/Zurich", "Europe/Zurich"),
    ("Europe/Paris", "Europe/Paris"),
    ("Europe/Amsterdam", "Europe/Amsterdam"),
    ("Europe/Brussels", "Europe/Brussels"),
    ("Europe/Luxembourg", "Europe/Luxembourg"),
    ("Europe/Rome", "Europe/Rome"),
    ("Europe/Madrid", "Europe/Madrid"),
    ("Europe/Lisbon", "Europe/Lisbon"),
    ("Europe/London", "Europe/London"),
    ("Europe/Dublin", "Europe/Dublin"),
    ("Europe/Copenhagen", "Europe/Copenhagen"),
    ("Europe/Stockholm", "Europe/Stockholm"),
    ("Europe/Oslo", "Europe/Oslo"),
    ("Europe/Helsinki", "Europe/Helsinki"),
    ("Europe/Warsaw", "Europe/Warsaw"),
    ("Europe/Prague", "Europe/Prague"),
    ("Europe/Bratislava", "Europe/Bratislava"),
    ("Europe/Budapest", "Europe/Budapest"),
    ("Europe/Ljubljana", "Europe/Ljubljana"),
    ("Europe/Zagreb", "Europe/Zagreb"),
    ("Europe/Sarajevo", "Europe/Sarajevo"),
    ("Europe/Belgrade", "Europe/Belgrade"),
    ("Europe/Sofia", "Europe/Sofia"),
    ("Europe/Bucharest", "Europe/Bucharest"),
    ("Europe/Athens", "Europe/Athens"),
    ("Europe/Tallinn", "Europe/Tallinn"),
    ("Europe/Riga", "Europe/Riga"),
    ("Europe/Vilnius", "Europe/Vilnius"),
]

DEPARTMENT_LOCALE_CHOICES = [
    ("de-DE", "Deutsch (Deutschland)"),
    ("de-AT", "Deutsch (Österreich)"),
    ("de-CH", "Deutsch (Schweiz)"),
    ("en-GB", "English (United Kingdom)"),
    ("fr-FR", "Français (France)"),
    ("it-IT", "Italiano (Italia)"),
    ("nl-NL", "Nederlands (Nederland)"),
    ("pl-PL", "Polski (Polska)"),
    ("cs-CZ", "Čeština (Česko)"),
    ("sk-SK", "Slovenčina (Slovensko)"),
    ("hu-HU", "Magyar (Magyarország)"),
    ("da-DK", "Dansk (Danmark)"),
    ("sv-SE", "Svenska (Sverige)"),
    ("nb-NO", "Norsk bokmål (Norge)"),
    ("fi-FI", "Suomi (Suomi)"),
    ("es-ES", "Español (España)"),
    ("pt-PT", "Português (Portugal)"),
]

DEFAULT_DEPARTMENT_TIMEZONE = "Europe/Berlin"
DEFAULT_DEPARTMENT_LOCALE = "de-DE"


def format_department_datetime(value: datetime | None, department) -> str:
    """Render an aware timestamp in a Department's policy without global activation."""
    if value is None:
        return "—"
    if django_timezone.is_naive(value):
        # Model timestamps are UTC-aware.  This preserves a useful, deterministic
        # presentation for legacy/template test values without changing storage.
        value = django_timezone.make_aware(value, UTC)
    local_value = value.astimezone(ZoneInfo(department.timezone))
    if department.locale == "en-GB":
        return local_value.strftime("%d %b %Y · %H:%M")
    if department.locale in {"fr-FR", "it-IT", "es-ES", "pt-PT"}:
        return local_value.strftime("%d/%m/%Y · %H:%M")
    return local_value.strftime("%d.%m.%Y · %H:%M")
