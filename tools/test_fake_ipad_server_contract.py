"""Check the external verifier against actual synthetic server file publications."""

# Imported pytest fixtures are intentionally injected by name below.
# ruff: noqa: F811

import io

import pytest

from apps.file_library import services
from apps.file_library.tests.test_publication import (  # noqa: F401
    crypto,
    installation_for,
    library_for,
    pdf,
    pdf_pipeline,
    publish,
    scope,
)
from apps.publications.document_v2 import process_next_generation_key_grant
from apps.publications.models import DocumentArtifact
from apps.publications.worker_grants import process_next_signed_manifest
from tools.fake_ipad.client import FakeIPadClient
from tools.fake_ipad.crypto import CryptoState
from tools.fake_ipad.output import Output
from tools.fake_ipad.state import DeviceState, secure_write
from tools.fake_ipad.transport import ApiClient, HttpResponse


@pytest.mark.django_db
@pytest.mark.parametrize("with_documents", [False, True])
def test_external_client_accepts_real_signed_file_generations(
    scope, pdf_pipeline, crypto, settings, client, tmp_path, monkeypatch, with_documents
):
    installation, private, credential, _ = installation_for(scope, settings)
    libraries = [library_for(scope), services.get_library(scope[1], station_id=scope[5].id)]
    for actor, library in zip(scope[:2], libraries, strict=True):
        services.create_folder(actor=actor, library_id=library.id, name="Unterlagen ü")
        if with_documents:
            services.upload_pdf(actor=actor, library_id=library.id, upload=pdf())
        publish(actor, library)
    state = DeviceState(tmp_path / "external-client")
    secure_write(state.private_key_path, CryptoState(private).private_pem())
    state.store_installation(
        installation_id=str(installation.id),
        tablet_id=str(installation.tablet_id),
        credential=credential,
        authorization_valid_until=installation.authorization_valid_until.isoformat(),
    )

    class DjangoTransport:
        expect = ApiClient.expect

        def call(self, method, path, *, bearer=None, headers=None, json_body=None):
            assert bearer == credential
            assert json_body in (None, {})
            django_headers = {
                f"HTTP_{key.upper().replace('-', '_')}": value
                for key, value in (headers or {}).items()
            }
            response = client.generic(
                method,
                path,
                data=b"{}" if json_body is not None else b"",
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {bearer}",
                **django_headers,
            )
            body = response.content
            if response.status_code == 202:
                if path == "/api/v1/tablet/manifest":
                    process_next_signed_manifest()
                else:
                    process_next_generation_key_grant()
            if response.status_code == 200 and "/artifacts/" in path:
                # The test transport performs nginx's X-Accel-Redirect only after
                # the real API has authorized this installation and artifact.
                artifact = DocumentArtifact.objects.get(pk=path.split("/")[-2])
                body = (settings.PUBLICATION_ARTIFACT_ROOT / artifact.artifact_path).read_bytes()
            return HttpResponse(
                response.status_code, {k.lower(): v for k, v in response.items()}, body, path
            )

    monkeypatch.setattr("tools.fake_ipad.client.time.sleep", lambda seconds: None)
    external = FakeIPadClient(state, DjangoTransport(), out=Output(stream=io.StringIO()))
    result = external.verify()
    assert set(result["datasets"]) == {"department_files", "station_files"}
    assert all(
        summary["documents"] == int(with_documents) for summary in result["datasets"].values()
    )
