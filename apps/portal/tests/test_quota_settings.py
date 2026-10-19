# ruff: noqa: F811
import re
from time import time

import pytest
from django.test import SimpleTestCase
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.file_library.models import DepartmentQuotaPolicy, Library
from apps.file_library.tests.test_libraries import scope  # noqa: F401
from apps.publications.models import PublicationJob

pytestmark = pytest.mark.django_db


def controls(response) -> dict[str, str]:
    """Read the actual rendered input values and selected options for a form round trip."""
    html = response.content.decode()
    values = {}
    for prefix in ("department", "station"):
        amount = re.search(rf'<input\b[^>]*name="{prefix}_amount"[^>]*>', html)
        assert amount is not None
        value = re.search(r'value="([^"]*)"', amount.group())
        assert value is not None
        values[f"{prefix}_amount"] = value[1]
        select = re.search(rf'<select\b[^>]*name="{prefix}_unit"[^>]*>(.*?)</select>', html, re.S)
        assert select is not None
        selected = re.search(r'<option value="([^"]*)" selected>', select[1])
        assert selected is not None
        values[f"{prefix}_unit"] = selected[1]
    return values


def reauthenticate(client):
    session = client.session
    session["recent_reauthentication_at"] = time()
    session.save()


def settings_url(scope):
    return reverse("portal-department-settings", args=(scope[3].id,))


def test_defaults_have_horizontal_controls_accessible_labels_and_usage(client, scope):
    client.force_login(scope[0])
    response = client.get(settings_url(scope))
    assert response.status_code == 200
    assert controls(response) == {
        "department_amount": "5",
        "department_unit": "GB",
        "station_amount": "2",
        "station_unit": "GB",
    }
    html = response.content.decode()
    form = response.context["quota_form"]
    for prefix, label in (("department", "Department files"), ("station", "Station files")):
        SimpleTestCase().assertInHTML(
            f'<div class="input-group flex-nowrap">'
            f"{form[f'{prefix}_amount']}{form[f'{prefix}_unit']}</div>",
            html,
            count=1,
        )
        SimpleTestCase().assertInHTML(
            f'<label class="form-label" for="id_{prefix}_amount">{label}</label>', html
        )
        SimpleTestCase().assertInHTML(
            f'<label class="visually-hidden" for="id_{prefix}_unit">{label} unit</label>', html
        )
        assert form[f"{prefix}_unit"].field.widget.attrs["style"] == "width: 100px;"
    assert "Current usage: 0\xa0bytes." in html
    assert "This limit applies separately to every station library." in html
    assert html.index("Save quotas") > html.index('name="station_unit"')


@pytest.mark.parametrize(
    ("quota_bytes", "amount", "unit"),
    [
        (5_000_000_000, "5", "GB"),
        (2_000_000_000, "2", "GB"),
        (1_073_741_824, "1.073741824", "GB"),
        (250_000_000, "250", "MB"),
        (9_876_543, "9.876543", "MB"),
        (0, "0", "MB"),
        (999_999_999, "999.999999", "MB"),
        (1_000_000_000, "1", "GB"),
        (1_099_511_627_776, "1099.511627776", "GB"),
    ],
)
def test_exact_values_render_and_round_trip_without_data_changes(
    client, scope, quota_bytes, amount, unit
):
    policy = DepartmentQuotaPolicy.objects.create(
        department=scope[3], department_files_bytes=quota_bytes, station_files_bytes=quota_bytes
    )
    library = Library.objects.create(department=scope[3], usage_bytes=123, source_revision=7)
    before = (AuditEvent.objects.count(), PublicationJob.objects.count())
    client.force_login(scope[0])
    response = client.get(settings_url(scope))
    values = controls(response)
    for prefix in ("department", "station"):
        assert values[f"{prefix}_amount"] == amount
        assert values[f"{prefix}_unit"] == unit
    policy.refresh_from_db()
    assert policy.department_files_bytes == policy.station_files_bytes == quota_bytes
    assert (AuditEvent.objects.count(), PublicationJob.objects.count()) == before
    reauthenticate(client)
    saved = client.post(settings_url(scope), {"action": "file-library-quota", **values})
    assert saved.status_code == 302
    assert saved.url == settings_url(scope)
    policy.refresh_from_db()
    library.refresh_from_db()
    assert policy.department_files_bytes == policy.station_files_bytes == quota_bytes
    assert (library.usage_bytes, library.source_revision) == (123, 7)
    assert (AuditEvent.objects.count(), PublicationJob.objects.count()) == before


def test_manual_units_keep_exact_byte_conversion(client, scope):
    client.force_login(scope[0])
    reauthenticate(client)
    values = {
        "action": "file-library-quota",
        "department_amount": "9.876543",
        "department_unit": "MB",
        "station_amount": "1.073741824",
        "station_unit": "GB",
    }
    assert client.post(settings_url(scope), values).status_code == 302
    policy = DepartmentQuotaPolicy.objects.get(department=scope[3])
    assert (policy.department_files_bytes, policy.station_files_bytes) == (9_876_543, 1_073_741_824)
    values.update(department_amount="0.009876543", department_unit="GB")
    values.update(station_amount="1073.741824", station_unit="MB")
    assert client.post(settings_url(scope), values).status_code == 302
    policy.refresh_from_db()
    assert (policy.department_files_bytes, policy.station_files_bytes) == (9_876_543, 1_073_741_824)


@pytest.mark.parametrize("amount", ["-1", "0.000000001", "1000000000000"])
def test_invalid_values_retain_input_and_inline_errors(client, scope, amount):
    client.force_login(scope[0])
    reauthenticate(client)
    values = {
        "department_amount": amount,
        "department_unit": "MB",
        "station_amount": "2",
        "station_unit": "GB",
    }
    response = client.post(settings_url(scope), {"action": "file-library-quota", **values})
    assert response.status_code == 200
    assert controls(response) == values
    html = response.content.decode()
    for error in response.context["quota_form"]["department_amount"].errors:
        SimpleTestCase().assertInHTML(f'<div class="invalid-feedback d-block">{error}</div>', html)
    assert response.context["quota_form"]["department_amount"].errors
    policy = DepartmentQuotaPolicy.objects.get(department=scope[3])
    assert policy.department_files_bytes == 5_000_000_000


def test_invalid_unit_has_labeled_feedback_and_preserves_amount(client, scope):
    client.force_login(scope[0])
    response = client.post(
        settings_url(scope),
        {
            "action": "file-library-quota",
            "department_amount": "5",
            "department_unit": "TB",
            "station_amount": "2",
            "station_unit": "GB",
        },
    )
    assert response.status_code == 200
    assert 'value="5"' in response.content.decode()
    errors = response.context["quota_form"]["department_unit"].errors
    assert errors
    for error in errors:
        SimpleTestCase().assertInHTML(
            f'<div class="invalid-feedback d-block">{error}</div>', response.content.decode()
        )


def test_quota_settings_require_department_authority_and_recent_reauthentication(client, scope):
    admin, station_admin, other_admin, department, *_ = scope
    policy = DepartmentQuotaPolicy.objects.create(department=department)
    values = {
        "action": "file-library-quota",
        "department_amount": "1",
        "department_unit": "MB",
        "station_amount": "1",
        "station_unit": "MB",
    }
    for user in (station_admin, other_admin):
        client.force_login(user)
        reauthenticate(client)
        assert client.get(settings_url(scope)).status_code == 403
        assert client.post(settings_url(scope), values).status_code == 403
    client.logout()
    assert client.get(settings_url(scope)).status_code == 302
    assert client.post(settings_url(scope), values).status_code == 302
    client.force_login(admin)
    response = client.post(settings_url(scope), values)
    assert response.status_code == 302
    assert response.url.startswith(reverse("accounts-reauthenticate"))
    policy.refresh_from_db()
    assert policy.department_files_bytes == 5_000_000_000
    assert policy.station_files_bytes == 2_000_000_000
