"""Pure bilingual assembly, using synthetic identities and independent goldens."""

import builtins
import hashlib
import importlib.util
import io
import json
import socket
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "deployment_eula_assembly", ROOT / "deploy/lib/eula.py"
)
assert spec is not None and spec.loader is not None
e = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = e
spec.loader.exec_module(e)

NOTICES = {
    "de": (
        "> Öffentliche Vorlage ohne Anbieteridentifikation — nicht zur Annahme bestimmt. "
        "Anlage B ist vor Annahme vollständig einzufügen.\n\n"
    ),
    "en": (
        "> Public template without Provider identification — not intended for acceptance. "
        "Annex B must be inserted in full before acceptance.\n\n"
    ),
}
HEADINGS = {
    "de": "## Anlage B — Anbieteridentifikation",
    "en": "## Annex B — Provider identification",
}
BLOCKS = {
    "de": HEADINGS["de"] + "\n\n[Extern bereitzustellen; vor Zustimmung vollständig einzufügen.]\n",
    "en": HEADINGS["en"] + "\n\n[To be supplied externally; insert in full before acceptance.]\n",
}
TEMPLATES = {
    "de": (
        "# Mustervertrag\n\n" + NOTICES["de"] + "\nVertragstext: ä.\n\n"
        "## Anlage A — Ausdrückliche Zustimmung\n\n"
        "- Betreiberorganisation (Rechtsträger): [einzutragen]\n"
        "- Vereinbarungsfassung und Revision: [einzutragen]\n"
        "- Stand: [einzutragen]\n\n" + BLOCKS["de"]
    ).encode("utf-8"),
    "en": (
        "# Synthetic agreement\n\n" + NOTICES["en"] + "\nContractual wording.\n\n"
        "## Annex A — Explicit acceptance\n\n"
        "- Operating Organization (legal entity): [to be entered]\n"
        "- Agreement version and revision: [to be entered]\n"
        "- Date: [to be entered]\n\n" + BLOCKS["en"]
    ).encode("utf-8"),
}
PROVIDERS = {
    "de": (HEADINGS["de"] + "\n\nSynthetischer Anbieter: Beispiel Prüfteam.\n").encode("utf-8"),
    "en": (HEADINGS["en"] + "\n\nSynthetic provider: Example Test Team.\n").encode("utf-8"),
}
# Literal expected complete documents, not generated with the assembly algorithm.
GOLDENS = {
    "de": (
        "# Mustervertrag\n\n\nVertragstext: ä.\n\n"
        "## Anlage A — Ausdrückliche Zustimmung\n\n"
        "- Betreiberorganisation (Rechtsträger): [einzutragen]\n"
        "- Vereinbarungsfassung und Revision: [einzutragen]\n"
        "- Stand: [einzutragen]\n\n"
        "## Anlage B — Anbieteridentifikation\n\n"
        "Synthetischer Anbieter: Beispiel Prüfteam.\n"
    ).encode(),
    "en": (
        "# Synthetic agreement\n\n\nContractual wording.\n\n"
        "## Annex A — Explicit acceptance\n\n"
        "- Operating Organization (legal entity): [to be entered]\n"
        "- Agreement version and revision: [to be entered]\n"
        "- Date: [to be entered]\n\n"
        "## Annex B — Provider identification\n\n"
        "Synthetic provider: Example Test Team.\n"
    ).encode(),
}


def digest(content):
    return hashlib.sha256(content).hexdigest()


def verified(templates=None, providers=None, **metadata):
    """Model the internal E.2 result, never a production verification shortcut."""
    templates = TEMPLATES if templates is None else templates
    providers = PROVIDERS if providers is None else providers
    records = tuple(
        e.LanguageRecord(
            language,
            f"EULA.public.{language}.md",
            digest(templates[language]),
            f"{e.ORIGIN}/legal/1.0-beta/PROVIDER.{language}.md",
            digest(providers[language]),
            f"{e.ORIGIN}/legal/1.0-beta/PROVIDER.{language}.md.minisig",
        )
        for language in ("de", "en")
    )
    manifest = e.Manifest(1, "1.0-beta", 2, 1, "2026-11-25", "approved", "de", "0" * 16, records)
    return e.VerifiedDocumentSet(
        replace(manifest, **metadata),
        "a" * 64,
        "b" * 64,
        tuple(
            e.VerifiedDocument(lang, templates[lang], providers[lang], b"synthetic-test-signature")
            for lang in ("de", "en")
        ),
    )


@pytest.mark.parametrize("language", ["de", "en"])
def test_exact_golden_document_and_hash(language):
    result = e.assemble_agreement_set(verified())
    agreement = result.german if language == "de" else result.english
    assert agreement.language == language
    assert agreement.content == GOLDENS[language]
    assert agreement.sha256 == digest(GOLDENS[language])
    assert agreement.content.count(HEADINGS[language].encode()) == 1
    assert agreement.content.endswith(PROVIDERS[language])
    assert (
        agreement.content.count(b"[einzutragen]" if language == "de" else b"[to be entered]") == 3
    )


def test_canonical_serialization_and_independent_golden_hashes():
    result = e.assemble_agreement_set(verified())
    # Filled from standalone hashlib over the literal golden documents below.
    assert result.german.sha256 == GOLDEN_DE_HASH
    assert result.english.sha256 == GOLDEN_EN_HASH
    assert result.canonical_bytes == GOLDEN_CANONICAL
    assert result.agreement_set_sha256 == GOLDEN_SET_HASH
    assert digest(result.canonical_bytes) == result.agreement_set_sha256
    identity = json.loads(result.canonical_bytes)
    assert set(identity) == {
        "schema_version",
        "agreement_version",
        "agreement_revision",
        "provider_revision",
        "assembled_sha256",
    }
    assert set(identity["assembled_sha256"]) == {"de", "en"}
    assert result.canonical_bytes == json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    assert not result.canonical_bytes.endswith(b"\n")


def test_repeat_order_independence_metadata_and_immutable_provenance():
    source = verified()
    result = e.assemble_agreement_set(source)
    assert e.assemble_agreement_set(source) == result
    reversed_source = replace(
        source,
        documents=source.documents[::-1],
        manifest=replace(source.manifest, languages=source.manifest.languages[::-1]),
    )
    assert e.assemble_agreement_set(reversed_source).canonical_bytes == result.canonical_bytes
    assert (result.agreement_version, result.agreement_revision, result.provider_revision) == (
        "1.0-beta",
        2,
        1,
    )
    assert result.effective_date == "2026-11-25"
    assert result.authoritative_language == "de"
    assert result.provenance is source
    for obj, field, value in (
        (result, "agreement_revision", 9),
        (result.german, "content", b"changed"),
        (source, "documents", ()),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, value)
    assert "Synthetischer" not in repr(result)
    assert "Synthetic provider" not in repr(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("agreement_revision", 3),
        ("agreement_revision", 2**31 - 1),
        ("provider_revision", 2),
        ("agreement_version", "2.0"),
    ],
)
def test_future_positive_revisions_and_metadata_bind_identity(field, value):
    baseline = e.assemble_agreement_set(verified())
    result = e.assemble_agreement_set(verified(**{field: value}))
    assert result.german == baseline.german
    assert result.english == baseline.english
    assert json.loads(result.canonical_bytes)[field] == value
    assert result.agreement_set_sha256 != baseline.agreement_set_sha256


@pytest.mark.parametrize("field", ["agreement_revision", "provider_revision"])
@pytest.mark.parametrize("value", [0, -1, True, "3"])
def test_invalid_revisions_fail_closed(field, value):
    with pytest.raises(e.EULAError, match="positive integer"):
        e.assemble_agreement_set(verified(**{field: value}))


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("part", ["provider", "template"])
def test_either_language_bytes_change_identity(language, part):
    templates, providers = dict(TEMPLATES), dict(PROVIDERS)
    if part == "provider":
        providers[language] += b"Changed synthetic detail.\n"
    else:
        templates[language] = b"Reviewed changed wording.\n" + templates[language]
    baseline = e.assemble_agreement_set(verified())
    result = e.assemble_agreement_set(verified(templates, providers))
    assert result.agreement_set_sha256 != baseline.agreement_set_sha256
    assert (result.german.sha256 == baseline.german.sha256) == (language != "de")
    assert (result.english.sha256 == baseline.english.sha256) == (language != "en")


def test_provenance_and_nonidentity_metadata_do_not_change_identity():
    source = verified()
    result = e.assemble_agreement_set(source)
    different_provenance = replace(
        source,
        manifest_sha256="c" * 64,
        public_key_sha256="d" * 64,
        documents=tuple(
            replace(doc, signature=b"different signature comment") for doc in source.documents
        ),
        manifest=replace(source.manifest, effective_date="2027-01-01"),
    )
    other = e.assemble_agreement_set(different_provenance)
    assert other.agreement_set_sha256 == result.agreement_set_sha256
    assert other.provenance is different_provenance
    assert other.effective_date == "2027-01-01"


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize(
    "mutation",
    [
        "no-notice",
        "duplicate-notice",
        "notice-no-blank",
        "inline-notice",
        "no-block",
        "duplicate-block",
        "nonterminal",
        "no-final-lf",
        "wrong-placeholder",
        "duplicate-heading",
        "other-heading",
        "altered-heading",
        "cross-notice",
        "extra-placeholder",
        "altered-notice",
        "inline-cross-heading",
    ],
)
def test_invalid_template_structure_no_partial_result(language, mutation):
    templates = dict(TEMPLATES)
    template = templates[language]
    notice = NOTICES[language].encode()
    block = BLOCKS[language].encode()
    heading = HEADINGS[language].encode()
    other = "en" if language == "de" else "de"
    changes = {
        "no-notice": template.replace(notice, b""),
        "duplicate-notice": notice + template,
        "notice-no-blank": template.replace(notice + b"\n", notice[:-1]),
        "inline-notice": template.replace(notice, b"prefix" + notice),
        "no-block": template[: -len(block)],
        "duplicate-block": template + block,
        "nonterminal": template + b"\nUnexpected suffix\n",
        "no-final-lf": template[:-1],
        "wrong-placeholder": template.replace(block, BLOCKS[other].encode()),
        "duplicate-heading": heading + b"\n" + template,
        "other-heading": HEADINGS[other].encode() + b"\n" + template,
        "altered-heading": b"### Annex B - altered heading\n" + template,
        "cross-notice": NOTICES[other].encode() + template,
        "extra-placeholder": b"[To be supplied externally; changed wording.]\n" + template,
        "altered-notice": b"> PUBLIC TEMPLATE: changed wording\n" + template,
        "inline-cross-heading": b"inline " + HEADINGS[other].encode() + b"\n" + template,
    }
    templates[language] = changes[mutation]
    with pytest.raises(e.EULAError) as caught:
        e.assemble_agreement_set(verified(templates))
    assert language in str(caught.value)
    assert "Contractual wording" not in str(caught.value)
    assert "Vertragstext" not in str(caught.value)


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize(
    "mutation",
    [
        "wrong-language",
        "duplicate-heading",
        "cross-heading",
        "lowercase-heading",
        "preface",
        "public-notice",
        "placeholder",
        "inline-duplicate",
        "inline-cross-heading",
    ],
)
def test_invalid_provider_structure(language, mutation):
    providers = dict(PROVIDERS)
    provider = providers[language]
    heading = HEADINGS[language].encode()
    other = "en" if language == "de" else "de"
    changes = {
        "wrong-language": PROVIDERS[other],
        "duplicate-heading": provider + heading + b"\n",
        "cross-heading": provider + HEADINGS[other].encode() + b"\n",
        "lowercase-heading": provider + b"## annex b - extra\n",
        "preface": b"Unexpected preface\n" + provider,
        "public-notice": provider + NOTICES[language].encode(),
        "placeholder": provider + b"[To be supplied externally; changed.]\n",
        "inline-duplicate": provider + b"inline " + heading + b"\n",
        "inline-cross-heading": provider + b"inline " + HEADINGS[other].encode() + b"\n",
    }
    providers[language] = changes[mutation]
    with pytest.raises(e.EULAError):
        e.assemble_agreement_set(verified(providers=providers))


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("tail", [b"", b"\n", b"\n\n", b"  \t\n\n", b"\r\n\t\n"])
def test_provider_whitespace_and_final_newline_are_not_normalized(language, tail):
    providers = dict(PROVIDERS)
    providers[language] = providers[language].rstrip(b"\n") + tail
    result = e.assemble_agreement_set(verified(providers=providers))
    agreement = result.german if language == "de" else result.english
    assert agreement.content == GOLDENS[language].rstrip(b"\n") + tail


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("part", ["template", "provider"])
@pytest.mark.parametrize("unsafe", [b"\xff", b"\x00", b"\x1b", "\u202e".encode()])
def test_invalid_text_rejected_without_content_in_diagnostics(language, part, unsafe):
    templates, providers = dict(TEMPLATES), dict(PROVIDERS)
    secret = b"SYNTHETIC-CONFIDENTIAL-DETAIL"
    target = templates if part == "template" else providers
    target[language] = secret + unsafe + target[language]
    with pytest.raises(e.EULAError, match="invalid UTF-8|unsafe control") as caught:
        e.assemble_agreement_set(verified(templates, providers))
    assert "SYNTHETIC-CONFIDENTIAL" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("mutation", ["de-only", "en-only", "duplicate", "list", "raw"])
def test_incomplete_or_alternative_input_rejected(mutation):
    source = verified()
    changes = {
        "de-only": replace(source, documents=source.documents[:1]),
        "en-only": replace(source, documents=source.documents[1:]),
        "duplicate": replace(source, documents=(source.documents[0], source.documents[0])),
        "list": replace(source, documents=list(source.documents)),
        "raw": PROVIDERS,
    }
    with pytest.raises(e.EULAError):
        e.assemble_agreement_set(changes[mutation])


@pytest.mark.parametrize("part", ["template", "provider"])
def test_input_must_still_match_verified_provenance(part):
    source = verified()
    modified = replace(source.documents[1], **{part: b"unverified replacement"})
    source = replace(source, documents=(source.documents[0], modified))
    with pytest.raises(e.EULAError, match="verified input hash mismatch"):
        e.assemble_agreement_set(source)


@pytest.mark.parametrize("language", ["de", "en"])
def test_approved_public_template_compatible_and_annex_a_untouched(language):
    templates = dict(TEMPLATES)
    templates[language] = (ROOT / f"legal/EULA.public.{language}.md").read_bytes()
    result = e.assemble_agreement_set(verified(templates))
    agreement = result.german if language == "de" else result.english
    original = templates[language]
    notice, block = NOTICES[language].encode(), BLOCKS[language].encode()
    assert original.count(notice) == 1
    assert original.endswith(block)
    assert (
        agreement.content == original.replace(notice, b"", 1)[: -len(block)] + PROVIDERS[language]
    )
    field = b"[einzutragen]" if language == "de" else b"[to be entered]"
    assert agreement.content.count(field) == original.count(field) == 3


def test_assembly_performs_no_io_or_execution(monkeypatch):
    source = verified()

    def forbidden(*args, **kwargs):
        raise AssertionError("Assembly attempted I/O or execution")

    with monkeypatch.context() as patch:
        for obj, name in (
            (builtins, "open"),
            (io, "open"),
            (Path, "open"),
            (e.os, "open"),
            (socket, "socket"),
            (socket, "create_connection"),
            (e.subprocess, "run"),
            (e, "verify_document_set"),
            (e, "CurlTransport"),
            (e.tempfile, "TemporaryDirectory"),
            (e.tempfile, "mkstemp"),
        ):
            patch.setattr(obj, name, forbidden)
        result = e.assemble_agreement_set(source)
    assert result.german.content == GOLDENS["de"]
    assert result.english.content == GOLDENS["en"]


def test_success_and_failure_do_not_log_document_contents(capsys, caplog):
    e.assemble_agreement_set(verified())
    providers = dict(PROVIDERS)
    providers["en"] += b"\n## Annex B - SYNTHETIC-CONFIDENTIAL-DETAIL\n"
    with pytest.raises(e.EULAError) as caught:
        e.assemble_agreement_set(verified(providers=providers))
    assert "SYNTHETIC-CONFIDENTIAL-DETAIL" not in str(caught.value)
    output = capsys.readouterr()
    assert output.out == output.err == ""
    assert caplog.text == ""


# Standalone-derived constants; never the owner's real provider documents.
GOLDEN_DE_HASH = "a0c3372e736309691d42dfd1195f53250c3ad132d7153fbd371dc5646869534f"
GOLDEN_EN_HASH = "9fbd86015f4ed9122d2a61fc23d760ec776613700d82ac57570614ff9e98c9bc"
GOLDEN_CANONICAL = (
    b'{"agreement_revision":2,"agreement_version":"1.0-beta","assembled_sha256":{'
    b'"de":"a0c3372e736309691d42dfd1195f53250c3ad132d7153fbd371dc5646869534f",'
    b'"en":"9fbd86015f4ed9122d2a61fc23d760ec776613700d82ac57570614ff9e98c9bc"},'
    b'"provider_revision":1,"schema_version":1}'
)
GOLDEN_SET_HASH = "05c227d482865cd0d5f04d86283df48383fdc784c2be564fb12250baf3cccc30"
