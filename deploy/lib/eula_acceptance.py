"""Isolated interactive acceptance. No installer integration or acceptance storage.

Production callers must supply assemble_agreement_set(verify_document_set(...)).
Python dataclasses are an internal trust boundary, not cryptographic credentials.
An accepted result retains the exact agreements/provenance for a future recorder;
software ref and resolved commit must be associated by that recorder, not inferred.
"""

import contextlib
import json
import os
import re
import signal
import stat
import threading
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, TextIO

from deploy.lib.eula import (
    AssembledAgreementSet,
    EULAError,
    Manifest,
    VerifiedDocumentSet,
    assemble_agreement_set,
    parse_manifest,
    require,
)


@dataclass(frozen=True)
class AcceptanceRecord:
    acceptance_id: str
    organization_legal_name: str = field(repr=False)
    representative_full_name: str = field(repr=False)
    representative_role: str = field(repr=False)
    authority_confirmed: bool
    annex_a_confirmed: bool
    agreement_version: str
    agreement_revision: int
    provider_revision: int
    effective_date: str
    german_sha256: str
    english_sha256: str
    agreement_set_sha256: str
    accepted_at: datetime
    presentation_language: str
    agreements: AssembledAgreementSet = field(repr=False)
    acceptance_method: Literal["interactive"] = field(default="interactive", init=False)


@dataclass(frozen=True)
class AcceptanceOutcome:
    status: Literal["accepted", "declined", "cancelled"]
    acceptance: AcceptanceRecord | None = field(default=None, repr=False)


def _safe(text: str, *, multiline: bool = False) -> str:
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeError:
        raise EULAError("Text is not valid UTF-8") from None
    require(
        not any(
            unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            and not (multiline and char in "\n\t")
            for char in text
        ),
        "Unsafe terminal control characters",
    )
    return text


def _document(content: bytes) -> str:
    try:
        return _safe(content.decode("utf-8"), multiline=True)
    except UnicodeError:
        raise EULAError("Agreement text is not valid UTF-8") from None


def validate_identity(value: str, *, representative: bool = False) -> str:
    """Self-declared identity only. Never include the rejected input in errors."""
    _safe(value)
    value = value.strip()
    require(2 <= len(value) <= 200, "Identity field must contain 2 to 200 characters")
    require(not representative or len(value.split()) >= 2, "Enter first and last name")
    return value


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
        require(
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None,
            "System clock unusable: an aware UTC timestamp is required; check the clock",
        )
        value = value.astimezone(UTC)
        require(2020 <= value.year <= 9998, "System clock unusable; check the date and time")
        return value
    except (OSError, OverflowError, TypeError, ValueError):
        raise EULAError("System clock unusable; check the date and time") from None


def validate_eligibility(agreements: AssembledAgreementSet, now: datetime) -> None:
    now = _utc_now(lambda: now)
    require(type(agreements) is AssembledAgreementSet, "A verified assembled agreement is required")
    require(type(agreements.provenance) is VerifiedDocumentSet, "Verified provenance is required")
    manifest = agreements.provenance.manifest
    require(type(manifest) is Manifest, "Verified manifest is required")
    require(manifest.status == "approved", "Agreement is not approved for acceptance")
    # Reuse the release schema validator rather than accepting loosely typed metadata.
    raw = asdict(manifest)
    raw["languages"] = {
        record.language: {key: value for key, value in asdict(record).items() if key != "language"}
        for record in manifest.languages
    }
    require(parse_manifest(json.dumps(raw).encode()) == manifest, "Inconsistent verified manifest")
    require(agreements.authoritative_language == "de", "German must be authoritative")
    require(
        re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", agreements.effective_date) is not None,
        "Invalid agreement effective date",
    )
    try:
        effective = date.fromisoformat(agreements.effective_date)
    except ValueError:
        raise EULAError("Invalid agreement effective date") from None
    require(effective <= now.date(), "Agreement is future-effective; check the system clock")
    require(
        all(
            type(document.signature) is bytes and 0 < len(document.signature) <= 8192
            for document in agreements.provenance.documents
        ),
        "Verified signature provenance is incomplete",
    )
    require(
        all(
            re.fullmatch(r"[0-9a-f]{64}", value) is not None
            for value in (
                agreements.provenance.manifest_sha256,
                agreements.provenance.public_key_sha256,
            )
        ),
        "Verified release provenance is incomplete",
    )
    require(
        assemble_agreement_set(agreements.provenance) == agreements,
        "Agreement metadata, content or hashes are inconsistent",
    )
    for agreement in (agreements.german, agreements.english):
        _document(agreement.content)
    _safe(agreements.agreement_version)


def annex_a_statement(agreements: AssembledAgreementSet, language: str) -> str:
    """Return the exact Annex A section, including its five unfilled fields."""
    require(language in ("de", "en"), "Unsupported presentation language")
    agreement = agreements.german if language == "de" else agreements.english
    text = _document(agreement.content)
    heading = (
        "## Anlage A — Ausdrückliche Zustimmung"
        if language == "de"
        else "## Annex A — Explicit acceptance"
    )
    end = (
        "## Anlage B — Anbieteridentifikation"
        if language == "de"
        else "## Annex B — Provider identification"
    )
    require(text.count(heading + "\n") == text.count(end + "\n") == 1, "Invalid Annex A structure")
    statement = text.split(heading + "\n", 1)[1].split(end + "\n", 1)[0]
    require(statement.endswith("\n\n---\n\n"), "Invalid Annex A termination")
    lines = statement.splitlines()
    numbered = [line for line in lines if re.match(r"[0-9]+\.", line)]
    require(
        len(numbered) == 10
        and all(
            line.startswith(f"{number}. ") and len(line) > 4
            for number, line in enumerate(numbered, 1)
        )
        and "§ 16a" in numbered[8],
        "Expected ten complete Annex A acknowledgments",
    )
    marker = "[einzutragen]" if language == "de" else "[to be entered]"
    fields = [line for line in lines if line.startswith("- ")]
    require(
        len(fields) == statement.count(marker) == 5
        and all(line.endswith(": " + marker) for line in fields)
        and statement.index(numbered[-1]) < statement.index(fields[0])
        and len([line for line in lines if line.startswith("**") and line.endswith("**")]) == 1,
        "Expected five unfilled Annex A fields and explicit acceptance statement",
    )
    return heading + "\n" + statement[: -len("\n\n---\n\n")]


class _ExportCleanupError(EULAError):
    """Cleanup could not complete; the operator must inspect the selected destination."""


def export_agreements(agreements: AssembledAgreementSet, destination: str) -> None:
    """Create a NEW private directory, never overwrite or follow destination links.

    Requires Unix dir_fd/O_NOFOLLOW semantics; fail closed on other platforms.
    On ordinary failure/interruption remove only files created in our owned dir.
    This explicit document export is not a persistent acceptance record.
    """
    require(
        type(agreements) is AssembledAgreementSet
        and assemble_agreement_set(agreements.provenance) == agreements,
        "Export requires consistent verified assembled agreements",
    )
    _safe(destination)
    path = Path(destination)
    require(path.is_absolute() and ".." not in path.parts, "Select a new absolute directory path")
    require(path.anchor != "//", "Select a local absolute directory without a double-slash root")
    source = Path(__file__).resolve().parents[2]
    require(path != source and source not in path.parents, "Export outside the source repository")
    require(
        os.name == "posix" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"),
        "Secure export requires native Unix filesystem protections",
    )
    # Open every ancestor relative to an already-open directory: no resolve/check/open race.
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    parent = None
    owned = None
    created = False
    written = []
    success = False
    try:
        parent = os.open(path.anchor, directory_flags)
        for component in path.parts[1:-1]:
            next_parent = os.open(component, directory_flags, dir_fd=parent)
            os.close(parent)
            parent = next_parent
        parent_stat = os.fstat(parent)
        require(
            parent_stat.st_uid == getattr(os, "geteuid", lambda: -1)()
            and not parent_stat.st_mode & 0o022,
            "Export parent must be owned by the current user and not writable by other users",
        )
        os.mkdir(path.name, mode=0o700, dir_fd=parent)
        created = True
        owned = os.open(path.name, directory_flags, dir_fd=parent)
        require(stat.S_IMODE(os.fstat(owned).st_mode) == 0o700, "Export directory is not private")
        for name, agreement in (
            ("EULA.de.md", agreements.german),
            ("EULA.en.md", agreements.english),
        ):
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600, dir_fd=owned
            )
            written.append(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(agreement.content)
                stream.flush()
                os.fsync(stream.fileno())
        success = True
    except OSError:
        raise EULAError(
            "Export failed: choose a new writable directory without symlinks or conflicts"
        ) from None
    finally:
        try:
            if owned is not None:
                try:
                    if not success:
                        for name in written:
                            os.unlink(name, dir_fd=owned)
                finally:
                    os.close(owned)
            if created and not success and parent is not None:
                os.rmdir(path.name, dir_fd=parent)
        except OSError:
            raise _ExportCleanupError(
                "Export cleanup failed; inspect the explicitly selected destination"
            ) from None
        finally:
            if parent is not None:
                os.close(parent)


@dataclass
class Terminal:
    reader: TextIO = field(repr=False)
    writer: TextIO = field(repr=False)

    def show(self, text: str) -> None:
        self.writer.write(_safe(text, multiline=True).replace("\t", "    ") + "\n")
        self.writer.flush()

    def ask(self, prompt: str) -> str:
        self.show(prompt)
        line = self.reader.readline(4098)
        if not line or len(line) > 4096 or not line.endswith("\n"):
            raise EOFError
        try:
            return _safe(line[:-1])
        except EULAError:
            raise _TerminalInputError("Invalid terminal input; no acceptance recorded") from None


class _TerminalInputError(EULAError):
    """Retryable input rejection, separate from failed legal verification."""


@contextlib.contextmanager
def _interaction_signals() -> Iterator[None]:
    require(
        threading.current_thread() is threading.main_thread(),
        "Interactive acceptance must run on the main thread for interruption-safe export cleanup",
    )
    previous = {}

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    try:
        for name in ("SIGTERM", "SIGHUP"):
            if hasattr(signal, name):
                signum = getattr(signal, name)
                previous[signum] = signal.signal(signum, interrupted)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextlib.contextmanager
def production_terminal() -> Iterator[Terminal]:
    try:
        with (
            open("/dev/tty", encoding="utf-8", errors="strict") as reader,
            open("/dev/tty", "w", encoding="utf-8", errors="strict") as writer,
        ):
            require(reader.isatty() and writer.isatty(), "Interactive terminal required")
            yield Terminal(reader, writer)
    except OSError:
        raise EULAError(
            "Interactive /dev/tty unavailable; supplementary acceptance was not recorded"
        ) from None


# Presentation text only; contractual wording is always extracted from Annex A.
_TEXT = {
    "warning": (
        "BETA: Fehler/Ausfälle können Einsatzverzögerungen, Einsatzversagen, Sachschäden, "
        "finanzielle Schäden, schwere Verletzungen oder Tod verursachen. FireDash darf keine "
        "unverzichtbare Einsatzkomponente sein. Unabhängige, dokumentierte und regelmäßig "
        "getestete Rückfalllösungen sind erforderlich. Die Zustimmung ist freiwillig; "
        "AGPL-Rechte bleiben unberührt. Die Systemzeit ist kein unabhängig "
        "vertrauenswürdiger Zeitnachweis.",
        "BETA: Defects/failures may contribute to operational delays, mission failure, property "
        "damage, financial loss, serious injury or death. FireDash must not be indispensable "
        "to operational readiness. Independent, documented and regularly tested fallback "
        "solutions are required. Acceptance is voluntary; AGPL rights remain unaffected. "
        "The system clock is not independently trusted time.",
    ),
    "menu": (
        "1 Deutsch vollständig lesen | 2 Englisch vollständig lesen | 3 Beide speichern | "
        "4 Zur ergänzenden Zustimmung | 5 Ablehnen | 0 Abbrechen",
        "1 Read complete German | 2 Read complete English | 3 Save both | "
        "4 Proceed to supplementary acceptance | 5 Decline | 0 Cancel",
    ),
    "privacy": (
        "§ 14(7–8): Organisation, Name und Funktion werden lokal als Nachweis der erklärten "
        "Zustimmung mit der exakten Vereinbarungsidentität aufbewahrt. Keine automatische "
        "Übermittlung an den FireDash-Anbieter. Identität und Vertretungsbefugnis sind "
        "Selbstauskünfte, nicht unabhängig geprüft. Erforderliche Datenschutzinformationen "
        "und Rechtsgrundlagen werden durch die Zustimmung nicht ersetzt. Ein Hosting-Anbieter "
        "oder externer Installateur darf nur mit entsprechender Vertretungsbefugnis für eine "
        "andere Organisation zustimmen. Diese Eingabe allein speichert noch keinen Nachweis.",
        "§ 14(7–8): Organization, representative name and role will be retained locally as "
        "evidence of declared acceptance, associated with the exact agreement identity. No "
        "automatic transmission to the FireDash Provider. Identity and authority are "
        "self-declared, not independently verified. Acceptance replaces neither an appropriate "
        "data protection legal basis nor required privacy information. A hosting provider or "
        "external installer may accept for another organization only with appropriate authority. "
        "Entering details alone does not store a record.",
    ),
    "organization": (
        "Vollständiger rechtlicher Name der Betreiberorganisation:",
        "Operating Organization full legal name:",
    ),
    "name": (
        "Vor- und Nachname der zustimmenden Person:",
        "Accepting representative first and last name:",
    ),
    "role": (
        "Funktion innerhalb der Organisation:",
        "Position or function within the organization:",
    ),
    "authority": (
        "Vertretungsbefugnis ausdrücklich bestätigen: JA (sonst zurück)",
        "Explicitly confirm representation authority: YES (otherwise return)",
    ),
    "final": (
        "AKZEPTIEREN = ausdrücklich zustimmen | K = Angaben korrigieren | Z = zurück",
        "ACCEPT = explicit acceptance | C = correct identity | B = back",
    ),
    "invalid": (
        "Ungültige Eingabe. Keine Zustimmung erfasst.",
        "Invalid input. No acceptance recorded.",
    ),
    "export": (
        "Neues absolutes Zielverzeichnis außerhalb des Quellcodes:",
        "New absolute destination directory outside the source repository:",
    ),
    "export_ok": (
        "Beide Vereinbarungen exakt exportiert.",
        "Both agreements exported as exact bytes.",
    ),
    "export_error": (
        "Export fehlgeschlagen. Unix-Dateisystemschutz ist erforderlich. Neues privates, "
        "beschreibbares Ziel außerhalb des Quellcodes ohne Verknüpfungen wählen; "
        "erneuter Versuch über 3.",
        "Export failed. Unix filesystem protections are required. Choose a new private, "
        "writable destination outside the source repository without links; retry with 3.",
    ),
    "export_cleanup": (
        "Exportbereinigung fehlgeschlagen. Das ausdrücklich gewählte Ziel auf unvollständige "
        "Dateien prüfen. Kein vollständiger Export bestätigt.",
        "Export cleanup failed. Inspect the explicitly selected destination for partial files. "
        "A complete export has not been confirmed.",
    ),
    "declined": (
        "Ergänzende Zustimmung abgelehnt. AGPL-Rechte bleiben unberührt. Ein gleichwertiger "
        "AGPL-only-Installationsweg wird verfügbar sein.",
        "Supplementary acceptance declined. AGPL rights remain unaffected. An equally functional "
        "AGPL-only installation route will be available.",
    ),
}


def _message(key: str, language: str) -> str:
    return _TEXT[key][int(language == "en")]


def _identity(terminal: Terminal, language: str) -> tuple[str, str, str] | None:
    terminal.show(_message("privacy", language))
    values = []
    for key in ("organization", "name", "role"):
        while True:
            try:
                values.append(
                    validate_identity(
                        terminal.ask(_message(key, language)), representative=key == "name"
                    )
                )
                break
            except EULAError:
                terminal.show(_message("invalid", language))
    if terminal.ask(_message("authority", language)) != ("JA" if language == "de" else "YES"):
        return None
    return values[0], values[1], values[2]


def _review(
    agreements: AssembledAgreementSet,
    terminal: Terminal,
    clock: Callable[[], datetime],
    started: datetime,
) -> AcceptanceOutcome:
    language = terminal.ask("Sprache: Enter = Deutsch; en = English; de = Deutsch")
    while language not in ("", "de", "en"):
        language = terminal.ask("Bitte de oder en wählen / Select de or en")
    language = language or "de"
    statements = {lang: annex_a_statement(agreements, lang) for lang in ("de", "en")}
    metadata = (
        f"Version: {agreements.agreement_version}; Revision: {agreements.agreement_revision}; "
        f"Provider revision / Anbieterrevision: {agreements.provider_revision}\n"
        f"Effective date / Stand: {agreements.effective_date}; "
        f"Authoritative / maßgeblich: Deutsch (de)\nSHA-256: {agreements.agreement_set_sha256}"
    )
    terminal.show(metadata)
    terminal.show(_message("warning", language))
    for document in agreements.provenance.documents:
        terminal.show(
            "Provider / Anbieter (" + document.language + "):\n" + _document(document.provider)
        )
    while True:
        try:
            choice = terminal.ask(_message("menu", language))
            if choice in ("1", "2"):
                terminal.show(
                    _document((agreements.german if choice == "1" else agreements.english).content)
                )
            elif choice == "3":
                try:
                    export_agreements(agreements, terminal.ask(_message("export", language)))
                    terminal.show(_message("export_ok", language))
                except _ExportCleanupError:
                    terminal.show(_message("export_cleanup", language))
                except EULAError:
                    terminal.show(_message("export_error", language))
            elif choice == "4":
                while True:
                    identity = _identity(terminal, language)
                    if identity is None:
                        terminal.show(_message("invalid", language))
                        break
                    terminal.show(metadata)
                    for key, value in zip(("organization", "name", "role"), identity, strict=True):
                        terminal.show(_message(key, language) + " " + value)
                    terminal.show(statements[language])
                    response = terminal.ask(_message("final", language))
                    if response == ("AKZEPTIEREN" if language == "de" else "ACCEPT"):
                        accepted_at = _utc_now(clock)
                        require(
                            accepted_at >= started, "System clock moved backwards; check the clock"
                        )
                        validate_eligibility(agreements, accepted_at)
                        record = AcceptanceRecord(
                            acceptance_id=str(uuid.uuid4()),
                            organization_legal_name=identity[0],
                            representative_full_name=identity[1],
                            representative_role=identity[2],
                            authority_confirmed=True,
                            annex_a_confirmed=True,
                            agreement_version=agreements.agreement_version,
                            agreement_revision=agreements.agreement_revision,
                            provider_revision=agreements.provider_revision,
                            effective_date=agreements.effective_date,
                            german_sha256=agreements.german.sha256,
                            english_sha256=agreements.english.sha256,
                            agreement_set_sha256=agreements.agreement_set_sha256,
                            accepted_at=accepted_at,
                            presentation_language=language,
                            agreements=agreements,
                        )
                        return AcceptanceOutcome("accepted", record)
                    if response != ("K" if language == "de" else "C"):
                        terminal.show(_message("invalid", language))
                        break
            elif choice == "5":
                terminal.show(_message("declined", language))
                return AcceptanceOutcome("declined")
            elif choice == "0":
                return AcceptanceOutcome("cancelled")
            else:
                terminal.show(_message("invalid", language))
        except _TerminalInputError:
            terminal.show(_message("invalid", language))


def run_interactive_acceptance(
    agreements: AssembledAgreementSet,
    *,
    terminal: Terminal | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AcceptanceOutcome:
    """No stdin fallback, logging, network or acceptance-record writes.

    EOF, ordinary interruption and terminal I/O failure cancel without identity,
    timestamps or affirmative records. Invalid/unverifiable inputs raise EULAError.
    """
    started = _utc_now(clock)
    validate_eligibility(agreements, started)
    # Both Annex A structures must be usable even if only one is presented.
    for language in ("de", "en"):
        annex_a_statement(agreements, language)
    try:
        with _interaction_signals():
            if terminal is not None:
                return _review(agreements, terminal, clock, started)
            with production_terminal() as tty:
                return _review(agreements, tty, clock, started)
    except (EOFError, KeyboardInterrupt, UnicodeError, OSError):
        return AcceptanceOutcome("cancelled")
