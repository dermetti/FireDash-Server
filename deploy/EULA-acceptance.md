# Isolated interactive acceptance

`deploy.lib.eula_acceptance.run_interactive_acceptance(agreements)` accepts the
immutable `AssembledAgreementSet` returned by the existing verified assembly
workflow. It does not retrieve documents, integrate with the installer or write
acceptance records. Production interaction uses `/dev/tty`, never piped stdin.
Tests can inject `Terminal(reader, writer)` and an aware datetime clock callable.

Eligibility reuses manifest schema validation and deterministic assembly to check
metadata, provenance and all hashes, requires approved status, German authority
and an effective date no later than the UTC system date. The system clock is not
independently trusted time. The workflow trusts the internal verification result;
manually constructing Python dataclasses does not authenticate documents.

Both complete languages are available through the review menu before proceeding.
Provider identification and safety warnings are displayed. Documents are plain
text without pagers, shell commands or HTML rendering. UTF-8 and unsafe terminal
controls are checked before display. Presentation does not alter agreement bytes.
The complete selected Annex A statement is extracted from those bytes, including
all ten acknowledgments and five unfilled fields. Reviewing or scrolling alone
never creates acceptance.

Example German interaction, after reviewing the agreements:

```text
Sprache: [Enter]
Auswahl: 4
Betreiberorganisation: Beispielorganisation e. V.
Vor- und Nachname: Alex Beispiel
Funktion: Leitung
Vertretungsbefugnis: JA
[Identität, Vereinbarungsidentität und vollständige Anlage A werden angezeigt]
Bestätigung: AKZEPTIEREN
```

Example English interaction:

```text
Language: en
Selection: 4
Operating Organization: Example Organization
First and last name: Alex Example
Role: Operations Manager
Representation authority: YES
[Identity, agreement identity and complete Annex A are displayed]
Confirmation: ACCEPT
```

These are abbreviated examples with synthetic identities, not contractual text.
Authority must be confirmed separately, including for hosting providers accepting
on another organization's behalf. The workflow explains local evidentiary retention
and the data protection responsibilities in § 14(7–8) before requesting identity.
Identity and authority remain self-declared. There is no additional GDPR consent
checkbox. At final confirmation, `K` (German) or `C` (English) permits correction
and requires a new authority confirmation. EOF or Ctrl-C cancels. The workflow
runs on the main thread, handles ordinary termination signals for export cleanup,
and restores prior signal handlers. Decline leaves
AGPL rights intact; the equally functional AGPL-only installer route is future work.

An accepted `AcceptanceOutcome` has `status="accepted"` and an immutable
`AcceptanceRecord` in `acceptance`. That record contains a UUID, all three declared
identity fields, the authority and Annex A confirmations, agreement metadata,
both language hashes, agreement-set hash, UTC `accepted_at`, presentation language
and `acceptance_method="interactive"`. Its `agreements` reference retains the exact
bytes and verified provenance. Identity, document contents and signatures are
excluded from default representations. Do not explicitly log them or dump locals.

Declined and cancelled outcomes have no record, identity, acceptance timestamp or
affirmative confirmations. Each successful call creates a separate record; no
single-organization-per-installation assumption is made. A future protected
recorder can associate this result with an explicitly requested software ref and
resolved release commit SHA. This stage neither invents that metadata nor stores
records, audit logs, customer acceptance or runtime licensing restrictions.

## Explicit document export

Menu option 3 exports exact bytes as `EULA.de.md` and `EULA.en.md` into a **new**,
explicitly selected absolute directory outside the source checkout. On Unix,
descriptor-relative operations reject symlink ancestors and existing destinations.
The immediate parent must belong to the current user and must not be writable by
other users. Directory permissions are 0700 and file creation permissions 0600.
Ordinary failures and Python interruptions remove files created by the failed
export. Export errors permit another attempt from the menu. Filesystem cleanup
errors require inspecting the selected destination. Platforms lacking the required
Unix protections fail closed. Export is an explicit retention option, not automatic
agreement or acceptance storage.

Native `/dev/tty` operation, Unix permissions and symlink behavior require Linux
validation. Automated tests use synthetic provider documents and identity details;
they never retrieve actual provider documents.
