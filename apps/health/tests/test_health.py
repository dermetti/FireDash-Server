from unittest.mock import MagicMock, patch

import pytest
from django.db.utils import OperationalError
from django.test import Client
from django.urls import reverse


@pytest.mark.django_db
def test_liveness_is_available_without_database_access(client: Client) -> None:
    response = client.get(reverse("health-live"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_readiness_checks_postgis(client: Client) -> None:
    response = client.get(reverse("health-ready"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
@patch("apps.health.views.connections")
def test_readiness_hides_database_errors(mock_connections: MagicMock, client: Client) -> None:
    mock_connections.__getitem__.side_effect = OperationalError("sensitive connection detail")

    response = client.get(reverse("health-ready"))

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
