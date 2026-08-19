"""Publication artifact byte ceiling: envelope coverage, boundary, and logging."""

import pytest
from django.test import override_settings

from apps.publications.builders import (
    ARTIFACT_BUILDERS,
    PublicationBuildError,
    build_artifact,
)
from apps.publications.registry import get_dataset_definition


def test_publication_artifact_ceiling_default_supports_fire_plan_envelope(settings):
    assert settings.PUBLICATION_ARTIFACT_MAX_BYTES == 600 * 1024 * 1024


def test_build_artifact_accepts_observed_fire_plan_size(monkeypatch):
    definition = get_dataset_definition("department_fire_plans")
    monkeypatch.setitem(
        ARTIFACT_BUILDERS,
        "department_fire_plans",
        lambda **kwargs: b"x" * (168 * 1024 * 1024),
    )
    artifact = build_artifact(
        definition=definition, department=None, station=None, source_revision=1
    )
    assert len(artifact) == 168 * 1024 * 1024


def test_build_artifact_rejects_above_ceiling_and_logs(monkeypatch, caplog):
    definition = get_dataset_definition("department_fire_plans")
    monkeypatch.setitem(
        ARTIFACT_BUILDERS,
        "department_fire_plans",
        lambda **kwargs: b"x" * 2048,
    )
    with override_settings(PUBLICATION_ARTIFACT_MAX_BYTES=1024):
        with pytest.raises(PublicationBuildError, match="configured maximum"):
            build_artifact(definition=definition, department=None, station=None, source_revision=1)

    assert any(
        "Publication artifact exceeds ceiling" in record.getMessage() for record in caplog.records
    )


def test_build_artifact_rejects_exactly_at_boundary_plus_one(monkeypatch):
    definition = get_dataset_definition("department_fire_plans")
    monkeypatch.setitem(
        ARTIFACT_BUILDERS,
        "department_fire_plans",
        lambda **kwargs: b"x" * 1025,
    )
    with override_settings(PUBLICATION_ARTIFACT_MAX_BYTES=1024):
        with pytest.raises(PublicationBuildError):
            build_artifact(definition=definition, department=None, station=None, source_revision=1)
