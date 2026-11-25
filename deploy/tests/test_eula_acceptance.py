"""Focused terminal acceptance tests; real public templates, synthetic identities only."""

import builtins
import io
import os
import signal
import socket
import stat
import subprocess
import uuid
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from deploy.lib import eula as e
from deploy.lib import eula_acceptance as a
from deploy.tests.proxy_infrastructure_check import contains_prohibited

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 25, 12, 34, 56, tzinfo=UTC)


def synthetic_agreements(*, mutate=None, **metadata):
    manifest = e.parse_manifest((ROOT / "legal/provider-manifest.json").read_bytes())
    documents, records = [], []
    for record in manifest.languages:
        template = (ROOT / "legal" / record.template).read_bytes()
        if mutate is not None:
            template = mutate(template, record.language)
        heading = (
            "## Anlage B — Anbieteridentifikation"
            if record.language == "de"
            else "## Annex B — Provider identification"
        )
        provider = (heading + "\n\nSynthetic Confidential Provider\n").encode()
        documents.append(
            e.VerifiedDocument(record.language, template, provider, b"synthetic signature")
        )
        records.append(
            replace(record, template_sha256=e.sha256(template), provider_sha256=e.sha256(provider))
        )
    manifest = replace(manifest, languages=tuple(records), **metadata)
    source = e.VerifiedDocumentSet(manifest, "a" * 64, "b" * 64, tuple(documents))
    return e.assemble_agreement_set(source)


@pytest.fixture
def agreements():
    return synthetic_agreements()


def dialogue(agreements, lines, **kwargs):
    output = io.StringIO()
    terminal = a.Terminal(io.StringIO("\n".join(lines) + "\n"), output)
    outcome = a.run_interactive_acceptance(
        agreements, terminal=terminal, clock=lambda: NOW, **kwargs
    )
    return outcome, output.getvalue()


def affirmative(language="de"):
    return [
        language,
        "4",
        "Synthetic Operating Organization",
        "Alex Example",
        "Duty Chief",
        "JA" if language != "en" else "YES",
        "AKZEPTIEREN" if language != "en" else "ACCEPT",
    ]


@pytest.mark.parametrize("language", ["de", "en"])
def test_success_exact_review_and_private_immutable_result(agreements, language, capsys, caplog):
    lines = affirmative(language)
    lines[1:1] = ["1", "2"]
    outcome, transcript = dialogue(agreements, lines)
    assert outcome.status == "accepted"
    record = outcome.acceptance
    assert record is not None
    assert record.organization_legal_name == "Synthetic Operating Organization"
    assert record.representative_full_name == "Alex Example"
    assert record.representative_role == "Duty Chief"
    assert record.authority_confirmed is record.annex_a_confirmed is True
    assert record.agreements is agreements
    assert (record.agreement_version, record.agreement_revision, record.provider_revision) == (
        "1.0-beta",
        3,
        1,
    )
    assert record.effective_date == "2026-11-25"
    assert record.german_sha256 == agreements.german.sha256
    assert record.english_sha256 == agreements.english.sha256
    assert record.agreement_set_sha256 == agreements.agreement_set_sha256
    assert record.accepted_at == NOW and record.accepted_at.tzinfo is UTC
    assert record.presentation_language == language and record.acceptance_method == "interactive"
    assert uuid.UUID(record.acceptance_id).version == 4
    for content in (agreements.german.content, agreements.english.content):
        assert content.decode() in transcript
    statement = a.annex_a_statement(agreements, language)
    assert statement in transcript
    assert "§ 16a" in statement
    assert statement.count("[einzutragen]" if language == "de" else "[to be entered]") == 5
    assert "Synthetic Confidential Provider" in transcript
    for private in (
        "Alex Example",
        "Duty Chief",
        "Synthetic Operating Organization",
        "Synthetic Confidential Provider",
        "synthetic signature",
    ):
        assert private not in repr(record) + repr(outcome)
    for target, attribute, value in (
        (record, "authority_confirmed", False),
        (outcome, "status", "declined"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(target, attribute, value)
    assert capsys.readouterr() == ("", "") and not caplog.records


def test_annex_is_literal_section_and_ten_acknowledgments(agreements):
    for language, agreement in (("de", agreements.german), ("en", agreements.english)):
        statement = a.annex_a_statement(agreements, language)
        assert statement.encode() in agreement.content
        for number in range(1, 11):
            assert statement.count(f"\n{number}. ") == 1


def test_privacy_precedes_identity_and_hosting_authority_explanation(agreements):
    _, text = dialogue(agreements, affirmative("en"))
    assert text.index("§ 14(7–8)") < text.index("Operating Organization full legal name:")
    for phrase in (
        "self-declared, not independently verified",
        "hosting provider",
        "appropriate authority",
        "legal basis",
        "required privacy information",
        "No automatic transmission",
        "retained locally",
        "independently trusted time",
    ):
        assert phrase in text


def test_default_german_and_unique_ids(agreements):
    first, _ = dialogue(agreements, affirmative(""))
    second, _ = dialogue(agreements, affirmative("de"))
    assert first.acceptance.presentation_language == "de"
    assert first.acceptance.acceptance_id != second.acceptance.acceptance_id


@pytest.mark.parametrize(
    "response", ["", "yes", "JA", "ACCEPT", "acceptieren", " AKZEPTIEREN", "AKZEPTIEREN "]
)
def test_ambiguous_final_never_accepts(agreements, response):
    lines = affirmative()
    lines[-1] = response
    outcome, _ = dialogue(agreements, lines + ["0"])
    assert outcome == a.AcceptanceOutcome("cancelled")


@pytest.mark.parametrize("authority", ["", "no", "ACCEPT", "AKZEPTIEREN", "yes"])
def test_authority_is_separate_and_explicit(agreements, authority):
    lines = affirmative()
    lines[-2] = authority
    outcome, _ = dialogue(agreements, lines + ["5"])
    assert outcome == a.AcceptanceOutcome("declined")


def test_identity_correction_requires_new_authority(agreements):
    lines = affirmative("en")[:-1] + [
        "C",
        "Corrected Organization",
        "Jamie Example",
        "Coordinator",
        "YES",
        "ACCEPT",
    ]
    outcome, text = dialogue(agreements, lines)
    assert outcome.acceptance.representative_full_name == "Jamie Example"
    assert text.count("Explicitly confirm representation authority") == 2


@pytest.mark.parametrize("choice,status", [("5", "declined"), ("0", "cancelled")])
def test_negative_outcomes_have_no_record_or_identity(agreements, choice, status):
    outcome, text = dialogue(agreements, ["en", choice])
    assert outcome.status == status and outcome.acceptance is None
    assert "Operating Organization full legal name:" not in text
    if choice == "5":
        assert "equally functional AGPL-only" in text and "AGPL rights remain unaffected" in text


@pytest.mark.parametrize("count", range(7))
def test_eof_at_every_acceptance_step(agreements, count):
    terminal = a.Terminal(
        io.StringIO("\n".join(affirmative()[:count]) + ("\n" if count else "")), io.StringIO()
    )
    assert a.run_interactive_acceptance(
        agreements, terminal=terminal, clock=lambda: NOW
    ) == a.AcceptanceOutcome("cancelled")


@pytest.mark.parametrize("error", [KeyboardInterrupt, OSError, UnicodeError])
def test_terminal_interruption_is_cancelled(agreements, error):
    class Reader(io.StringIO):
        def readline(self, size=-1):
            raise error

    outcome = a.run_interactive_acceptance(
        agreements, terminal=a.Terminal(Reader(), io.StringIO()), clock=lambda: NOW
    )
    assert outcome == a.AcceptanceOutcome("cancelled")


def test_ordinary_termination_cancels_and_restores_handler(agreements):
    previous = signal.getsignal(signal.SIGTERM)

    class Reader(io.StringIO):
        def readline(self, size=-1):
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)

    outcome = a.run_interactive_acceptance(
        agreements, terminal=a.Terminal(Reader(), io.StringIO()), clock=lambda: NOW
    )
    assert outcome == a.AcceptanceOutcome("cancelled")
    assert signal.getsignal(signal.SIGTERM) == previous


def test_production_uses_only_two_tty_streams(agreements, monkeypatch):
    class Stream(io.StringIO):
        def isatty(self):
            return True

        def close(self):
            pass

    reader = Stream("\n".join(affirmative()) + "\n")
    writer = Stream()
    calls = []

    def open_tty(path, mode="r", **kwargs):
        assert path == "/dev/tty"
        assert kwargs == {"encoding": "utf-8", "errors": "strict"}
        calls.append(mode)
        return writer if mode == "w" else reader

    monkeypatch.setattr(builtins, "open", open_tty)
    outcome = a.run_interactive_acceptance(agreements, clock=lambda: NOW)
    assert outcome.status == "accepted" and calls == ["r", "w"]


def test_missing_tty_never_falls_back_to_stdin(agreements, monkeypatch):
    def unavailable(*args, **kwargs):
        assert args[0] == "/dev/tty"
        raise OSError("synthetic sensitive error")

    monkeypatch.setattr(builtins, "open", unavailable)
    with pytest.raises(e.EULAError, match="/dev/tty unavailable") as failure:
        a.run_interactive_acceptance(agreements, clock=lambda: NOW)
    assert "synthetic sensitive" not in str(failure.value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "x",
        "x" * 201,
        "Name\x00",
        "Name\x1b[2J",
        "Name\rOverwrite",
        "Name\u202e",
        "Name\u200b",
        "Name\u2028",
        "Name\ud800",
    ],
)
def test_invalid_identity_has_safe_diagnostics(value):
    with pytest.raises(e.EULAError) as failure:
        a.validate_identity(value)
    assert "Name" not in str(failure.value)


def test_invalid_identity_can_be_corrected(agreements):
    outcome, _ = dialogue(
        agreements,
        [
            "en",
            "4",
            "",
            "Synthetic Organization",
            "Single",
            "Alex Example",
            "\x1b[2J",
            "Chief",
            "YES",
            "ACCEPT",
        ],
    )
    assert outcome.status == "accepted"
    assert outcome.acceptance.representative_role == "Chief"


def test_control_in_menu_and_long_input_never_accept(agreements):
    outcome, text = dialogue(agreements, ["en", "\x1b[2J", "0"])
    assert outcome.acceptance is None and "\x1b" not in text
    outcome, _ = dialogue(agreements, ["en", "x" * 5000, "ACCEPT"])
    assert outcome.status == "cancelled"


@pytest.mark.parametrize(
    "clock_value", [None, datetime(2026, 11, 25), datetime(1970, 1, 1, tzinfo=UTC)]
)
def test_unusable_clock(agreements, clock_value):
    with pytest.raises(e.EULAError, match="clock unusable"):
        a.run_interactive_acceptance(
            agreements, terminal=a.Terminal(io.StringIO(), io.StringIO()), clock=lambda: clock_value
        )


def test_clock_exception_and_backwards_clock(agreements):
    def broken():
        raise OSError("sensitive clock environment")

    with pytest.raises(e.EULAError, match="clock unusable"):
        a.run_interactive_acceptance(agreements, clock=broken)
    times = iter((NOW, NOW - timedelta(seconds=1)))
    with pytest.raises(e.EULAError, match="moved backwards"):
        a.run_interactive_acceptance(
            agreements,
            terminal=a.Terminal(io.StringIO("\n".join(affirmative()) + "\n"), io.StringIO()),
            clock=lambda: next(times),
        )


def test_timestamp_converts_to_utc(agreements):
    local = NOW.astimezone(timezone(timedelta(hours=2)))
    outcome = a.run_interactive_acceptance(
        agreements,
        terminal=a.Terminal(io.StringIO("\n".join(affirmative()) + "\n"), io.StringIO()),
        clock=lambda: local,
    )
    assert outcome.acceptance is not None
    assert outcome.acceptance.accepted_at == NOW and outcome.acceptance.accepted_at.tzinfo is UTC


@pytest.mark.parametrize(
    "metadata,match",
    [
        ({"status": "draft"}, "not approved"),
        ({"effective_date": "2099-01-01"}, "future-effective"),
        ({"effective_date": "2026-02-30"}, "date"),
        ({"authoritative_language": "en"}, "German"),
    ],
)
def test_ineligible_agreements(metadata, match):
    agreements = synthetic_agreements(**metadata)
    with pytest.raises(e.EULAError, match=match):
        dialogue(agreements, affirmative())


@pytest.mark.parametrize(
    "field,value",
    [
        ("agreement_set_sha256", "f" * 64),
        ("canonical_bytes", b"{}"),
        ("agreement_revision", 42),
        ("provider_revision", 42),
        ("effective_date", "2026-11-24"),
    ],
)
def test_inconsistent_identity_rejected(agreements, field, value):
    with pytest.raises(e.EULAError, match="inconsistent"):
        dialogue(replace(agreements, **{field: value}), affirmative())


def test_modified_complete_document_and_missing_signature_rejected(agreements):
    tampered = replace(
        agreements,
        english=replace(agreements.english, content=agreements.english.content + b"Modified"),
    )
    with pytest.raises(e.EULAError, match="inconsistent"):
        dialogue(tampered, affirmative())
    document = replace(agreements.provenance.documents[0], signature=b"")
    source = replace(
        agreements.provenance, documents=(document, agreements.provenance.documents[1])
    )
    with pytest.raises(e.EULAError, match="signature provenance"):
        dialogue(replace(agreements, provenance=source), affirmative())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace(b"10. ", b"11. "),
        lambda text: text.replace(b"[to be entered]", b"[missing]", 1),
        lambda text: text.replace(b"9. ", b""),
        lambda text: text.replace("## Annex A — Explicit acceptance".encode(), b"## Changed Annex"),
    ],
)
def test_bad_annex_in_either_language_fails_before_interaction(mutation):
    agreements = synthetic_agreements(
        mutate=lambda text, language: mutation(text) if language == "en" else text
    )
    with pytest.raises(e.EULAError, match="Annex A"):
        dialogue(agreements, affirmative())


def test_future_revision_not_hardcoded():
    outcome, _ = dialogue(
        synthetic_agreements(agreement_revision=7, provider_revision=2), affirmative()
    )
    assert outcome.acceptance.agreement_revision == 7 and outcome.acceptance.provider_revision == 2


def test_terminal_rejects_carriage_return_in_verified_document():
    agreements = synthetic_agreements(mutate=lambda text, language: text.replace(b"\n", b"\r\n", 1))
    with pytest.raises(e.EULAError, match="control"):
        dialogue(agreements, affirmative())


def test_workflow_has_no_network_or_acceptance_storage(agreements, monkeypatch, caplog):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network or filesystem access")

    for target, name in (
        (builtins, "open"),
        (Path, "open"),
        (os, "open"),
        (socket, "socket"),
        (socket, "create_connection"),
        (subprocess, "run"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    outcome, _ = dialogue(agreements, affirmative())
    assert outcome.status == "accepted" and not caplog.records


def test_export_failure_retry_and_exact_set(agreements, monkeypatch):
    calls = []

    def export(value, destination):
        assert value is agreements
        calls.append(destination)
        if len(calls) == 1:
            raise e.EULAError("synthetic failure must not be displayed")

    monkeypatch.setattr(a, "export_agreements", export)
    outcome, text = dialogue(agreements, ["en", "3", "/first", "3", "/second", "5"])
    assert calls == ["/first", "/second"]
    assert outcome.status == "declined" and "retry with 3" in text
    assert "synthetic failure" not in text and "exact bytes" in text


def test_export_cleanup_failure_is_explicit_without_private_diagnostics(agreements, monkeypatch):
    def fail(*args):
        raise a._ExportCleanupError("Synthetic private error")

    monkeypatch.setattr(a, "export_agreements", fail)
    outcome, text = dialogue(agreements, ["en", "3", "/synthetic", "0"])
    assert outcome.status == "cancelled"
    assert "Inspect the explicitly selected destination" in text
    assert "Synthetic private error" not in text


@pytest.mark.parametrize(
    "destination",
    ["relative", "../elsewhere", str(ROOT / "agreements"), "/tmp/../unsafe", "/tmp/\x1b"],
)
def test_unsafe_export_destinations(agreements, destination):
    with pytest.raises(e.EULAError):
        a.export_agreements(agreements, destination)


@pytest.mark.skipif(os.name != "posix", reason="Double-slash root aliases use Unix path semantics")
def test_double_slash_cannot_bypass_source_export_guard(agreements):
    with pytest.raises(e.EULAError, match="double-slash"):
        a.export_agreements(agreements, "/" + str(ROOT / "synthetic-export"))


@pytest.mark.skipif(
    os.name != "posix", reason="Secure export requires Unix dir_fd/O_NOFOLLOW semantics"
)
def test_secure_export_exact_bytes_permissions_and_conflicts(agreements, tmp_path):
    # Pytest scratch is inside the source tree; use a test-only source-root override.
    # The public API must still reject exports into the actual source root.
    destination = tmp_path / "export"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(a, "__file__", "/synthetic/source/deploy/lib/eula_acceptance.py")
        a.export_agreements(agreements, str(destination))
        assert stat.S_IMODE(destination.stat().st_mode) == 0o700
        for language, agreement in (("de", agreements.german), ("en", agreements.english)):
            path = destination / f"EULA.{language}.md"
            assert path.read_bytes() == agreement.content
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(e.EULAError):
            a.export_agreements(agreements, str(destination))


@pytest.mark.skipif(os.name != "posix", reason="Requires Unix symlink and descriptor protections")
@pytest.mark.parametrize("failure", ["second_write", "interrupt", "symlink", "ancestor_symlink"])
def test_export_cleanup_and_symlinks(agreements, tmp_path, monkeypatch, failure):
    monkeypatch.setattr(a, "__file__", "/synthetic/source/deploy/lib/eula_acceptance.py")
    destination = tmp_path / "export"
    if failure in ("symlink", "ancestor_symlink"):
        outside = tmp_path / "untouched"
        outside.mkdir()
        link = tmp_path / "link"
        link.symlink_to(outside, target_is_directory=True)
        destination = link if failure == "symlink" else link / "export"
        with pytest.raises(e.EULAError):
            a.export_agreements(agreements, str(destination))
        assert not list(outside.iterdir())
        return
    original = os.open

    def fail_second(path, *args, **kwargs):
        if path == "EULA.en.md":
            if failure == "interrupt":
                raise KeyboardInterrupt
            raise OSError("synthetic private failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_second)
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else e.EULAError):
        a.export_agreements(agreements, str(destination))
    assert not destination.exists()


@pytest.fixture
def proxy_tree(tmp_path):
    for name in (
        "deploy/lib/eula.py",
        "deploy/lib/other.py",
        "deploy/install-local.sh",
        "docs/configuration.md",
        "docs/deployment.md",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic public fixture\n")
    return tmp_path


def test_approved_origin_exception_only(proxy_tree):
    (proxy_tree / "deploy/lib/eula.py").write_text('ORIGIN = "https://firedash-web.mjblab.de"\n')
    assert not contains_prohibited(proxy_tree)


def test_duplicate_approved_declaration_is_not_another_exception(proxy_tree):
    (proxy_tree / "deploy/lib/eula.py").write_text(
        'ORIGIN = "https://firedash-web.mjblab.de"\n' * 2
    )
    assert contains_prohibited(proxy_tree)


def test_cache_directory_is_not_a_source_scan_exception(proxy_tree):
    path = proxy_tree / "deploy/lib/__pycache__/unexpected.py"
    path.parent.mkdir()
    path.write_text("# mjblab proxy infrastructure\n")
    assert contains_prohibited(proxy_tree)


def test_missing_document_set_is_ineligible(agreements):
    with pytest.raises(e.EULAError, match="verified assembled agreement"):
        dialogue(None, affirmative())
    source = replace(agreements.provenance, documents=agreements.provenance.documents[:1])
    with pytest.raises(e.EULAError, match="German and English"):
        dialogue(replace(agreements, provenance=source), affirmative())


def test_nonacceptance_never_allocates_identifier(agreements, monkeypatch):
    def forbidden():
        raise AssertionError("Identifier allocated without acceptance")

    monkeypatch.setattr(a.uuid, "uuid4", forbidden)
    for lines in (["en", "5"], ["en", "0"], affirmative("en")[:-1]):
        outcome, _ = dialogue(agreements, lines)
        assert outcome.acceptance is None


def test_export_unavailable_platform_fails_closed(agreements, monkeypatch):
    monkeypatch.setattr(a.os, "name", "nt")
    # Use a fixed Path constructor: changing os.name otherwise changes Path dispatch on Unix.
    monkeypatch.setattr(a, "Path", type(ROOT))
    with pytest.raises(e.EULAError, match="native Unix"):
        a.export_agreements(agreements, str(ROOT.parent / "synthetic-export"))


@pytest.mark.skipif(os.name != "posix", reason="Requires native Unix parent-directory permissions")
def test_export_rejects_shared_writable_parent(agreements, tmp_path, monkeypatch):
    monkeypatch.setattr(a, "__file__", "/synthetic/source/deploy/lib/eula_acceptance.py")
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    with pytest.raises(e.EULAError, match="not writable by other users"):
        a.export_agreements(agreements, str(parent / "export"))
    assert not list(parent.iterdir())


@pytest.mark.parametrize(
    "name,line",
    [
        ("deploy/lib/eula.py", "# mjblab proxy\n"),
        ("deploy/lib/eula.py", 'ORIGIN = "https://firedash-web.mjblab.de" # squid\n'),
        ("deploy/lib/other.py", 'ORIGIN = "https://firedash-web.mjblab.de"\n'),
        ("docs/deployment.md", "100.64.0.8"),
        ("deploy/install-local.sh", "TAILSCALE"),
        ("docs/configuration.md", "headscale"),
        ("deploy/lib/other.py", "squid"),
    ],
)
def test_other_proxy_occurrences_still_rejected(proxy_tree, name, line):
    (proxy_tree / "deploy/lib/eula.py").write_text('ORIGIN = "https://firedash-web.mjblab.de"\n')
    with (proxy_tree / name).open("a") as stream:
        stream.write(line)
    assert contains_prohibited(proxy_tree)


def test_current_proxy_scan_and_ci_integration():
    assert not contains_prohibited(ROOT)
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "deploy/tests/test_eula_acceptance.py" in workflow
    shell = (ROOT / "deploy/tests/test_outbound_mail_proxy_env.sh").read_text()
    assert '"$ROOT/deploy/tests/proxy_infrastructure_check.py" "$ROOT"' in shell
