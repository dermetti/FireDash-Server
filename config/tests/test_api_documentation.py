"""Public API docs use pinned local executable assets and preserve schema routing."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.staticfiles import finders
from django.urls import reverse


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and "src" in attributes:
            self.urls.append(attributes["src"])
        if tag == "link" and attributes.get("rel") in ("stylesheet", "icon"):
            self.urls.append(attributes["href"])


def test_public_swagger_uses_only_discoverable_local_assets(client):
    response = client.get(reverse("openapi-docs"))
    assert response.status_code == 200
    assets = Assets()
    assets.feed(response.content.decode())
    assert len(assets.urls) == 4  # bundle, preset, CSS, favicon
    assert "cdn.jsdelivr.net" not in response.content.decode()
    assert "@latest" not in response.content.decode()
    for url in assets.urls:
        assert url is not None
        parsed = urlsplit(url)
        assert not parsed.scheme and not parsed.netloc
        assert parsed.path.startswith(settings.STATIC_URL + "drf_spectacular_sidecar/")
        found = finders.find(parsed.path.removeprefix(settings.STATIC_URL))
        assert found and Path(found).is_file()


def test_openapi_schema_route_remains_public_and_valid(client):
    response = client.get(reverse("openapi-schema"), HTTP_ACCEPT="application/vnd.oai.openapi+json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["openapi"] == "3.1.0"
    assert schema["paths"]
    assert schema["info"]["title"] == "FireDash Provisioning API"
