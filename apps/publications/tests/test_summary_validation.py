import uuid
from typing import cast

import pytest
from django.test import override_settings

from apps.publications.builders import (
    MAX_HYDRANT_STATUS_BUCKETS,
    PublicationBuildError,
    validate_summary,
)
from apps.publications.registry import get_dataset_definition


def _summary(code: str) -> dict[str, object]:
    summaries: dict[str, dict[str, object]] = {
        "department_hydrants": {
            "active_count": 1,
            "source_revision": 1,
            "status_counts": {"ACTIVE": 1},
        },
        "department_fire_plans": {
            "active_document_count": 1,
            "total_accepted_bytes": 1,
            "total_pages": 1,
            "source_revision": 1,
        },
        "station_personnel": {
            "person_count": 1,
            "station_id": str(uuid.uuid4()),
            "commander_eligible_count": 1,
            "verified_commander_email_count": 1,
            "source_revision": 1,
        },
        "test_department_incidents": {"incident_count": 1, "source_revision": 1},
    }
    return summaries[code]


def _validate(code: str, summary: dict[str, object]) -> None:
    validate_summary(definition=get_dataset_definition(code), summary=summary)


def test_registered_summary_schema_distinguishes_counts_from_metrics():
    assert get_dataset_definition("department_hydrants").summary_schema == {
        "active_count": "item_count",
        "source_revision": "non_negative_integer",
        "status_counts": "bounded_string_integer_map",
    }
    assert get_dataset_definition("department_fire_plans").summary_schema == {
        "active_document_count": "item_count",
        "total_accepted_bytes": "non_negative_integer",
        "total_pages": "non_negative_integer",
        "source_revision": "non_negative_integer",
    }
    assert get_dataset_definition("station_personnel").summary_schema == {
        "person_count": "item_count",
        "station_id": "uuid",
        "commander_eligible_count": "item_count",
        "verified_commander_email_count": "item_count",
        "source_revision": "non_negative_integer",
    }
    assert get_dataset_definition("test_department_incidents").summary_schema == {
        "incident_count": "item_count",
        "source_revision": "non_negative_integer",
    }


def test_fire_plan_summary_allows_large_non_count_metrics():
    summary = _summary("department_fire_plans")
    summary.update(
        {
            "total_accepted_bytes": 1_200_000,
            "total_pages": 10_001,
            "source_revision": 10_001,
        }
    )

    _validate("department_fire_plans", summary)


@pytest.mark.parametrize(
    ("code", "field"),
    (
        ("department_hydrants", "active_count"),
        ("department_fire_plans", "active_document_count"),
        ("station_personnel", "person_count"),
        ("station_personnel", "commander_eligible_count"),
        ("station_personnel", "verified_commander_email_count"),
        ("test_department_incidents", "incident_count"),
    ),
)
@override_settings(PUBLICATION_BUILD_SUMMARY_MAX_ITEMS=3)
def test_item_counts_remain_bounded(code, field):
    summary = _summary(code)
    summary[field] = 4

    with pytest.raises(PublicationBuildError, match="item limit"):
        _validate(code, summary)


@pytest.mark.parametrize(
    ("code", "field"),
    (
        ("department_fire_plans", "active_document_count"),
        ("department_fire_plans", "total_pages"),
    ),
)
@pytest.mark.parametrize("invalid_value", (-1, "1", True))
def test_count_and_metric_values_reject_negative_and_non_integer_values(code, field, invalid_value):
    summary = _summary(code)
    summary[field] = invalid_value

    with pytest.raises(PublicationBuildError, match="invalid summary value"):
        _validate(code, summary)


@override_settings(PUBLICATION_BUILD_SUMMARY_MAX_ITEMS=3)
def test_bounded_string_integer_map_values_are_item_counts():
    summary = _summary("department_hydrants")
    summary["status_counts"] = {"ACTIVE": 3}
    _validate("department_hydrants", summary)

    summary["status_counts"] = {"ACTIVE": 4}
    with pytest.raises(PublicationBuildError, match="item limit"):
        _validate("department_hydrants", summary)


def test_bounded_string_integer_map_retains_category_and_value_validation():
    summary = _summary("department_hydrants")
    summary["status_counts"] = {str(index): 1 for index in range(MAX_HYDRANT_STATUS_BUCKETS + 1)}
    with pytest.raises(PublicationBuildError, match="category limit"):
        _validate("department_hydrants", summary)

    summary["status_counts"] = {"ACTIVE": True}
    with pytest.raises(PublicationBuildError, match="invalid summary value"):
        _validate("department_hydrants", summary)


def test_hydrant_summary_supports_50000_items_by_default():
    definition = get_dataset_definition("department_hydrants")
    for count in (38_000, 50_000):
        validate_summary(
            definition=definition,
            summary={
                "active_count": count,
                "source_revision": 1,
                "status_counts": {"ACTIVE": count},
            },
        )


def test_hydrant_summary_still_bounded_above_50000():
    definition = get_dataset_definition("department_hydrants")
    with pytest.raises(PublicationBuildError, match="item limit"):
        validate_summary(
            definition=definition,
            summary={
                "active_count": 50_001,
                "source_revision": 1,
                "status_counts": {"ACTIVE": 50_001},
            },
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.slow
@pytest.mark.parametrize("hydrant_count", [38_000, 50_000])
def test_large_hydrant_department_builds_summary(hydrant_count):
    from django.contrib.gis.geos import Point

    from apps.accounts.models import User
    from apps.organizations.models import Department
    from apps.publications.builders import build_summary
    from apps.reference_data.models import Hydrant

    user = User.objects.create_user("builder@example.test", "Builder", "safe-password")
    department = Department.objects.create(name="Large", short_code="LRG", created_by=user)
    Hydrant.objects.bulk_create(
        Hydrant(
            department=department,
            external_identifier=f"H-{i}",
            location=Point(8.0, 50.0, srid=4326),
            status="ACTIVE",
        )
        for i in range(hydrant_count)
    )

    summary = build_summary(
        definition=get_dataset_definition("department_hydrants"),
        department=department,
        station=None,
        source_revision=1,
    )

    assert summary["active_count"] == hydrant_count
    status_counts = cast(dict[str, int], summary["status_counts"])
    assert status_counts["ACTIVE"] == hydrant_count
