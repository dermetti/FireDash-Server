# Phase G targeted remediation

Reviewed baseline: `a422d808178a79b3aebb3362ec176efb2b036fb8`.

## Changed files

- `deploy/lib/tls.py`
- `deploy/lib/verify_systemd.py` (new)
- `deploy/systemd/fire-pdf-sanitizer@.service`
- `deploy/verify-deployment.sh`
- `deploy/tests/test_tls.py`
- `deploy/tests/test_verify_systemd.py` (new)
- `.github/workflows/ci.yml`
- `docs/security-phase-g-remediation.md` (this report)

## FD-SEC-013: Debian default-site ownership

The TLS helper recognizes only `sites-enabled/default` symlinks resolving to
`sites-available/default`, with a regular, non-symlink target. It requires
nginx-common's installed status and the target's matching conffile checksum from
`dpkg-query`. There is no embedded package-version checksum. MD5 is used only
to compare dpkg's recorded conffile identity, with `usedforsecurity=False`.
The path is listed in Debian 13's
[nginx-common package inventory](https://packages.debian.org/trixie/all/nginx-common/filelist).

Only this recognized enabled link is removed. The available file is preserved.
Modified defaults, unexpected enabled files/targets and ordinary conflicting
custom wildcard default listeners produce sanitized errors requiring manual
review. Other vhosts, including defaults on different ports or specific IPs,
are preserved. Ownership and ordinary enabled-vhost conflicts are checked during
the existing TLS preparation phase, before maintenance when already present.
The final `nginx -t` remains authoritative for includes and configuration syntax;
the early check is deliberately not a replacement Nginx parser.

Reconciliation belongs to the existing site transaction. Validation, activation
or reload failure restores the prior site, FireDash enabled-link state and exact
packaged-link target. After an attempted reload of an active Nginx, recovery also
attempts validation/reload of the restored configuration; the original error
still fails installation. Repeated convergence needs no packaged enabled link.
The HTTP-01 temporary site uses the same policy and now also declares the
intentional IPv4/IPv6 default listeners. Production routing and logging locations
are unchanged.

Tests cover ownership failures, absent/disabled defaults, unrelated vhosts,
preflight conflicts, repeat convergence and validation/reload/activation rollback.
The native routing test now includes a disposable Debian-style packaged default
fixture and removes its link through the actual reconciliation transaction,
while retaining the high-port HTTP-01 and redirect assertions. A separate Linux
test uses real installed dpkg metadata and a disposable copy of the conffile;
it never changes the installed Nginx configuration. These native tests were not
executed on this Windows host.

## FD-SEC-025: optional obsolete PDF path

Only the legacy `InaccessiblePaths` entry changed to
`-/var/lib/fire-backend/publications-tmp`. The required publications parent
continues to hide current `publications/.tmp` storage. No obsolete directory is
created. All other mounts, identities, restrictions and limits are unchanged.
A deployment regression checks the optional legacy entry and required protected
paths.

## FD-SEC-027: effective systemd checks

The deployment verifier calls a standard-library-only helper that queries
`systemctl show --all --property=...`, checking property names as well as values.
Missing, failed, unsafe and unprintable essential values fail verification.
Checks cover the principal web/publication/maintenance/PDF identities and
NoNewPrivileges, PDF worker/broker PrivateNetwork and absence of application
environment, worker memory/task/CPU/start-time limits, socket users/groups/modes,
and broker accepted-connection stdio. Template instances are inspected without
starting a job. Pending daemon reloads fail verification.

Debian 13/systemd 257 exposes structured credential properties that systemctl
cannot render reliably. For those, the helper checks ordered `systemctl cat`
fragments/drop-ins, honoring list resets and continuations, requiring exact
expected credential identifiers/paths and excluding other credential sources.
Empty EnvironmentFiles arrays also produce no property line in systemd 257,
even with `--all`, so PDF EnvironmentFile declarations use this fallback too.
It checks NeedDaemonReload before and after this inspection. Ambiguous escapes
and specifiers fail closed. This retained fragment-based boundary is not a
direct read of the manager's structured credential properties. Existing actual
credential metadata, running-web credential namespace, filesystem containment
and native PDF smoke checks remain required and unchanged.

The verifier additionally checks the actual Gunicorn socket identity/mode;
the existing broker socket filesystem check remains. No drop-in is changed.
Expected effective values are the shipped Debian 13 unit values (including
536870912 bytes, 32 tasks, CPU quota `1s`, start timeout `1min`, booleans `yes`
and socket mode `0660`). Custom security settings or credential layouts require
explicit review rather than automatic rewriting. Command/schedule and narrow
write-path textual checks outside the principal security properties remain;
this task does not claim comprehensive effective systemd hardening.

Tests supply manager responses independently of the matching shipped base
directives, reject unsafe overrides and missing/uninspectable properties, and
check credential resets/conflicts and retained native checks. These fixtures
are not native systemd acceptance. Property names/rendering were checked against
the [Debian 13 D-Bus documentation](https://manpages.debian.org/trixie/systemd/org.freedesktop.systemd1.5.en.html)
and [systemd 257's property printer](https://github.com/systemd/systemd/blob/v257/src/systemctl/systemctl-show.c).

## FD-SEC-026: accepted transition

The pre-Phase-E to unified-PDF upgrade can briefly install helpers whose internal
contract differs from the running application. Perform that transition during
planned maintenance without concurrent administrative PDF uploads. The owner
accepts this non-blocking transitional limitation. Installer phases, quiescence
and broker protocols are unchanged; no FD-SEC-026-specific tests were added.

## Focused local validation

- TLS, effective systemd and PDF deployment tests: **210 passed, 37 skipped**.
- Deployment configuration tests: **180 passed, 1 skipped** (part of an earlier
  combined run: 372 passed, 38 skipped, before the final verifier/test changes).
- The final run includes **72 passing** verifier/optional-path cases.
- Four shell regressions passed: verifier Django/PDF smoke failure propagation,
  Nginx ETag, Nginx upload boundary and systemd quiescence.
- Changed Python Ruff lint/format, Linux-targeted mypy and Bandit passed.
  Windows-targeted mypy initially reported existing POSIX-only OS APIs;
  `--platform linux` matches these deployment components' supported target.
- `bash -n deploy/verify-deployment.sh` passed. ShellCheck 0.11.0 found no added
  diagnostics versus baseline (90 existing diagnostics in each version).
- YAML parsing and whitespace checks passed.

An initial new test assertion named a nonexistent smoke script; it was corrected
to assert the existing `verify_library_pdf_sanitizer` management command. Final
focused runs have no failures. Native symlink/permission, Nginx, installed-package,
Restic and inherited-flock tests were skipped where unavailable on Windows.
Neither the complete application suite nor dependency scans were run.

## Baseline CI

GitHub API inspection confirmed all five jobs in
[run 45621028455](https://github.com/dermetti/FireDash-Server/actions/runs/45621028455)
completed successfully: server-tests, deployment-security, static-checks,
dependency-audit and acme-environment. The final server log reports **2,197 passed,
1 skipped**. No baseline regression was found. This is baseline evidence, not
CI or native acceptance of the changes in this working tree. The existing
deployment-security pytest command now includes the new effective-systemd tests
and PDF deployment contract checks.

## Remaining native release acceptance

No live server, deployment or operational data was used. Native Nginx and Debian
13/systemd execution are unavailable locally. Linux CI must execute the new
symlink/package/routing tests. Before release, perform:

- Fresh Debian 13 amd64 installation of an exact reviewed SHA, correct packaged
  default-site reconciliation and repeated installer convergence.
- Public HTTP-01 issuance, managed TLS staging validation and unattended renewal,
  while preserving manual/DNS-01 modes, lineage adoption and recovery procedures.
- The controlled old-PDF transition described above; actual systemd PDF processing
  for Fire Plans, KLGV and File Library with credential/filesystem containment.
- Actual effective systemd property inspection on Debian 13, including unsafe
  drop-in rejection and the optional missing legacy path.
- Real Nginx/Gunicorn client-IP attribution and setup-token/Magic-Link logging
  confidentiality, including early failures/default-host handling.
- Effective host firewall and SSH exposure verification, and applicable backup
  and recovery acceptance.

Deployment acceptance is mandatory. Independently optional TLS backup acceptance
remains separate and is not made a mandatory installation prerequisite. No Git
mutations or deployment were performed.
