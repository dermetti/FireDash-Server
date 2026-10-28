"""Console reports using synthetic transport and genuine cryptographic verification."""

import io
import json
from contextlib import contextmanager

import pytest

from tools.fake_ipad import cli
from tools.fake_ipad.errors import ClientError
from tools.fake_ipad.output import Output, verification_statistics
from tools.fake_ipad.test_generations import delivery, nine  # noqa: F401
from tools.fake_ipad.transport import ApiClient

# Imported fixtures intentionally share the existing complete wire-format factory.
# ruff: noqa: F811

NAMES = (
    "Dangerous Goods",
    "Hydrants",
    "Personnel",
    "Department Phonebook",
    "Station Phonebook",
    "Department Files",
    "Station Files",
    "Fire Plans",
    "KLGV Plans",
)


def test_default_report_all_nine_and_real_download_counts(nine):
    nine.client.verify()
    text = nine.stream.getvalue()
    assert all(name in text for name in NAMES)
    assert "Installation: ACTIVE" in text
    assert "Manifest:     PASS (Ed25519, ETag)" in text
    assert "Datasets:     9" in text
    assert "VERIFY RESULT: PASS" in text
    assert "HTTP 200" not in text
    assert "HTTP 304" not in text
    assert "/artifacts/" not in text
    assert "ciphertext SHA-256" not in text
    assert "HPKE" not in text
    assert len(text.splitlines()) < 40
    for wire in nine.generations.values():
        for document in wire["documents"]:
            assert document["artifact_id"] not in text
    stats = nine.client.last_verification_statistics
    assert stats["downloaded_verified"] == {"SYSTEM": 1, "DEPARTMENT": 8, "STATION": 4}
    assert stats["cached_verified"] == dict.fromkeys(stats["downloaded_verified"], 0)
    # 13 authenticated downloads and 13 conditional tests actually still ran.
    calls = [headers for _, path, headers in nine.calls if path in nine.artifacts]
    assert len(calls) == 26
    assert sum("If-None-Match" in headers for headers in calls) == 13


def test_verbose_still_reports_crypto_and_individual_artifacts(nine):
    nine.client.out.verbose = True
    nine.client.verify()
    text = nine.stream.getvalue()
    for label in ("HPKE", "AES-KW", "AES-GCM", "SHA-256", "If-None-Match", "304"):
        assert label in text
    for wire in nine.generations.values():
        for document in wire["documents"]:
            assert document["artifact_id"] in text


@pytest.mark.parametrize("command", ["download", "manifest", "refresh", "update-check"])
def test_cached_report_never_claims_new_downloads(nine, command):
    nine.client.verify()
    nine.stream.seek(0)
    nine.stream.truncate()
    before = len(nine.calls)
    if command == "manifest":
        result = nine.client.manifest(download=True, if_none_match=nine.etag)
    elif command == "refresh":
        result = nine.client.refresh()["sync"]
    elif command == "update-check":
        result = nine.client.update_check(
            expect_changed=[], expect_unchanged=[], expect_version_increase=[]
        )
    else:
        result = nine.client.download()
    assert result == {"changed": False}
    text = nine.stream.getvalue()
    assert "UNCHANGED / PASS" in text
    assert "CACHED / PASS" in text
    assert "13 previously verified artifacts reused" in text
    assert all(name in text for name in NAMES)
    assert not any(path in nine.artifacts for _, path, _ in nine.calls[before:])
    assert nine.client.last_verification_statistics == {
        "downloaded_verified": {"SYSTEM": 0, "DEPARTMENT": 0, "STATION": 0},
        "cached_verified": {"SYSTEM": 1, "DEPARTMENT": 8, "STATION": 4},
    }


@pytest.mark.parametrize("command", ["download", "manifest", "refresh"])
def test_compact_report_for_other_full_commands(nine, command):
    if command == "manifest":
        nine.client.manifest(download=True)
    else:
        getattr(nine.client, command)()
    text = nine.stream.getvalue()
    assert all(name in text for name in NAMES)
    assert "DOWNLOAD SUMMARY" in text
    assert "/artifacts/" not in text
    assert "ciphertext SHA-256" not in text
    assert "HTTP 304" not in text


def test_error_context_on_second_pdf_does_not_count_incomplete_generation(delivery):
    descriptor, wire = delivery.add_v2("department_fire_plans", count=3)
    delivery.seal_main()
    document = wire["documents"][1]
    path = document["download_path"]
    ciphertext, digest, ranges = delivery.artifacts[path]
    delivery.artifacts[path] = (bytes([ciphertext[0] ^ 1]) + ciphertext[1:], digest, ranges)
    original_call = delivery.call

    def call(*args, **kwargs):
        response = original_call(*args, **kwargs)
        response.headers["x-request-id"] = "synthetic-request-2"
        return response

    delivery.call = call
    with pytest.raises(ClientError, match="ciphertext SHA-256") as caught:
        delivery.client.verify()
    delivery.client.out.error(caught.value)
    text = delivery.stream.getvalue()
    for value in (
        "department_fire_plans",
        "DEPARTMENT",
        descriptor["publication_id"],
        document["artifact_id"],
        document["fire_plan"]["id"],
        "ciphertext_sha256",
        "HTTP: 200",
        "synthetic-request-2",
        "Verified before failure: 1 of 3 documents",
    ):
        assert value in text
    assert "VERIFY RESULT: PASS" not in text
    assert "DOWNLOAD SUMMARY" not in text
    assert not delivery.client.last_verification_statistics
    assert "verified_snapshot" not in delivery.state.state


@pytest.mark.parametrize("mode", [[], ["--verbose"], ["--json"], ["--json", "--verbose"]])
def test_cli_modes_keep_json_compatible_and_secrets_private(nine, monkeypatch, capsys, mode):
    def build_client(args, out):
        nine.client.out = out
        return nine.state, nine.client

    monkeypatch.setattr(cli, "build_client", build_client)
    assert cli.main(["verify", *mode]) == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "synthetic-credential" not in combined
    assert "synthetic sanitized PDF" not in combined
    assert "M\u00fcller" not in combined  # phonebook plaintext
    if "--json" in mode:
        result = json.loads(captured.out)
        assert result["ok"] is True
        assert result["command"] == "verify"
        assert len(result["datasets"]) == 9
        assert "VERIFY RESULT: PASS" in captured.err
    else:
        assert "VERIFY RESULT: PASS" in captured.out
        assert not captured.err


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_failure_is_always_visible_and_json_is_single_document(
    delivery, monkeypatch, capsys, json_mode
):
    descriptor, _ = delivery.add_v2("station_files")
    delivery.seal_main()
    delivery.failures[descriptor["manifest_url"]] = 403

    def build_client(args, out):
        delivery.client.out = out
        return delivery.state, delivery.client

    monkeypatch.setattr(cli, "build_client", build_client)
    assert cli.main(["verify", *(["--json"] if json_mode else [])]) == 1
    captured = capsys.readouterr()
    diagnostics = captured.err if json_mode else captured.out
    assert "VERIFY RESULT: FAIL" in diagnostics
    assert "DATASET: station_files" in diagnostics
    assert "Stage: generation_manifest" in diagnostics
    assert "HTTP: 403" in diagnostics
    if json_mode:
        result = json.loads(captured.out)
        assert result["ok"] is False
        assert "error" in result


@pytest.mark.parametrize("verbose", [False, True])
def test_transport_details_are_centralized_and_redacted(verbose):
    stream = io.StringIO()
    out = Output(verbose=verbose, stream=stream)
    api = ApiClient("https://tablet.example", verbose=verbose, out=out)

    class Response:
        status = 200
        headers = {"Content-Type": "application/json", "X-Request-ID": "request-123"}

        def read(self):
            return json.dumps(
                {"credential": "do-not-print-response", "private_key": "private-secret"}
            ).encode()

    class Opener:
        @contextmanager
        def open(self, request, timeout):
            assert request.get_header("Authorization") == "Bearer do-not-print-bearer"
            yield Response()

    api._opener = Opener()
    api.call(
        "POST",
        "/api/v1/tablet/test",
        bearer="do-not-print-bearer",
        json_body={"token": "do-not-print-request"},
    )
    text = stream.getvalue()
    assert not any(
        value in text
        for value in (
            "do-not-print-response",
            "private-secret",
            "do-not-print-bearer",
            "do-not-print-request",
        )
    )
    if verbose:
        for label in (
            "POST",
            "HTTP 200",
            "request-123",
            "request JSON",
            "response JSON",
            "redacted",
        ):
            assert label in text
    else:
        assert text == ""


def test_artifact_count_uses_format_not_number_of_pdfs_in_legacy_archive():
    manifest = {
        "datasets": [
            {
                "type": "department_fire_plans",
                "scope": {"type": "DEPARTMENT"},
                "artifact_format": "zip",
            },
            {
                "type": "station_files",
                "scope": {"type": "STATION"},
                "artifact_format": "document-manifest-v2",
            },
        ]
    }
    results = {"department_fire_plans": {"fire_plans": 61}, "station_files": {"documents": 0}}
    assert verification_statistics(manifest, results)["downloaded_verified"] == {
        "SYSTEM": 0,
        "DEPARTMENT": 1,
        "STATION": 0,
    }


@pytest.mark.parametrize("count", [0, 61])
def test_pdf_report_length_is_independent_of_document_count(delivery, count):
    delivery.add_v2("department_fire_plans", count=count)
    delivery.seal_main()
    delivery.client.verify()
    text = delivery.stream.getvalue()
    assert f"{count} PDFs" in text
    assert len(text.splitlines()) < 30
    assert delivery.client.last_verification_statistics["downloaded_verified"] == {
        "SYSTEM": 0,
        "DEPARTMENT": count,
        "STATION": 0,
    }
    assert len([call for call in delivery.calls if call[1] in delivery.artifacts]) == count * 2


def test_main_manifest_failure_has_accurate_diagnostics(nine, monkeypatch, capsys):
    nine.manifest["signature"] = "A" * 88

    # The signature fails before the changed payload's ETag is considered.
    def build_client(args, out):
        nine.client.out = out
        return nine.state, nine.client

    monkeypatch.setattr(cli, "build_client", build_client)
    assert cli.main(["verify"]) == 1
    text = capsys.readouterr().out
    assert "Manifest: FAIL" in text
    assert "Stage: main_manifest" in text
    assert "VERIFY RESULT: FAIL" in text
    assert "VERIFY RESULT: PASS" not in text


@pytest.mark.parametrize("json_mode", [False, True])
def test_nested_key_material_is_redacted_in_all_diagnostics(json_mode, capsys):
    from tools.fake_ipad.output import pretty

    out = Output(json_mode=json_mode, verbose=True)
    secrets = {
        "credential": "credential-secret",
        "private_key": "private-secret",
        "CEK": "cek-secret",
        "KEK": "kek-secret",
        "wrapped_generation_key": "hpke-secret",
        "generation_wrapped_cek": "kw-secret",
        "Authorization": "bearer-secret",
    }
    payload = {"nested": [secrets]}
    out.detail(pretty(payload))
    out.emit(payload)
    captured = capsys.readouterr()
    assert not any(value in captured.out + captured.err for value in secrets.values())
    if json_mode:
        assert isinstance(json.loads(captured.out), dict)


def test_hydrant_samples_never_print_decrypted_locations():
    from tools.fake_ipad.datasets import validate_hydrants

    stream = io.StringIO()
    out = Output(verbose=True, stream=stream)
    document = {
        "type": "FeatureCollection",
        "schema_version": 1,
        "source_revision": 1,
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [11.2345678, 48.9876543]},
                "properties": {
                    "external_identifier": "confidential-hydrant-id",
                    "hydrant_type": "private-type",
                    "diameter_mm": 80,
                    "status": "active",
                },
            }
        ],
    }
    result = validate_hydrants(json.dumps(document).encode(), {"schema_version": 1}, out)
    assert result["features"] == 1
    text = stream.getvalue()
    for private in ("11.2345678", "48.9876543", "confidential-hydrant-id", "private-type"):
        assert private not in text
