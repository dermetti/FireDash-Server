"""Command-line interface for the fake iPad client."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tools.fake_ipad.client import FakeIPadClient
from tools.fake_ipad.errors import ClientError, fail
from tools.fake_ipad.output import Output
from tools.fake_ipad.state import DEFAULT_STATE_DIR, DeviceState
from tools.fake_ipad.transport import ApiClient
from tools.fake_ipad.validation import require_app_build, require_app_version


def get_token(args: argparse.Namespace, *, prompt: str) -> str:
    token: str | None = getattr(args, "token", None)
    token_file: str | None = getattr(args, "token_file", None)

    if token_file == "-":  # nosec B105
        token = sys.stdin.read().strip()
    elif token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            fail(f"Cannot read token file {token_file!r}: {exc}")
    elif token is None:
        token = getpass.getpass(prompt)

    if not token:
        fail("Token cannot be empty")
    return token


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--server",
        "--base-url",
        dest="server",
        help="FireDash API origin, e.g. https://firedash.example.org. "
        "Required on first adoption; later read from state.",
    )
    parser.add_argument(
        "--state-dir",
        default=DEFAULT_STATE_DIR,
        help=f"Persistent fake-iPad test state directory (default: {DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--app-version",
        help=(
            "Persist this fake app version (MAJOR.MINOR.PATCH); "
            "defaults to stored identity or 1.0.0."
        ),
    )
    parser.add_argument(
        "--app-build",
        type=int,
        help="Persist this positive fake application build number.",
    )
    parser.add_argument(
        "--clear-app-build",
        action="store_true",
        help="Clear the locally configured build number when changing fake app identity.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP request timeout seconds (default: 30)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="LAB ONLY: disable TLS certificate verification / permit HTTP",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print redacted request/response JSON as well as the normal test report",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a single machine-readable JSON result on stdout (diagnostics go to stderr)",
    )
    parser.add_argument(
        "--save-plaintext",
        action="store_true",
        help="Save decrypted dataset artifacts under the state directory for inspection",
    )


def add_token_arguments(parser: argparse.ArgumentParser, *, alias: str | None = None) -> None:
    options = ["--token"]
    if alias:
        options.append(alias)
    parser.add_argument(*options, dest="token", help="Out-of-band invitation token.")
    parser.add_argument(
        "--token-file",
        help="Read the invitation token from a file (use '-' for stdin). "
        "Avoids process-list exposure.",
    )
    parser.add_argument(
        "--provisioning-payload",
        help="FireDash QR JSON payload (firedash-provisioning-v1).",
    )


def apply_provisioning_payload(args: argparse.Namespace) -> None:
    payload_text: str | None = getattr(args, "provisioning_payload", None)
    if payload_text is None:
        return
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as error:
        fail(f"Provisioning payload is not JSON: {error}")
    if not isinstance(payload, dict) or set(payload) != {"origin", "protocol", "token"}:
        fail("Provisioning payload must contain exactly origin, protocol, and token.")
    origin, protocol, token = payload["origin"], payload["protocol"], payload["token"]
    parsed = urlsplit(origin) if isinstance(origin, str) else None
    if (
        protocol != "firedash-provisioning-v1"
        or not isinstance(token, str)
        or not token
        or parsed is None
        or parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        fail("Provisioning payload is not a valid firedash-provisioning-v1 HTTPS origin/token.")
    if args.server and args.server.rstrip("/") != origin.rstrip("/"):
        fail("--server conflicts with the provisioning payload origin.")
    if args.token and args.token != token:
        fail("--token conflicts with the provisioning payload token.")
    args.server = origin.rstrip("/")
    args.token = token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fake_ipad",
        description="FireDash fake iPad external backend acceptance client",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    adopt = sub.add_parser(
        "adopt",
        aliases=["provision"],
        help="Adopt a fake tablet using an out-of-band invitation token",
    )
    add_common_arguments(adopt)
    add_token_arguments(adopt, alias="--adoption-token")
    adopt.add_argument(
        "--no-verify", action="store_true", help="Skip post-adoption full verification"
    )
    adopt.add_argument(
        "--simulate-lost-completion-response",
        action="store_true",
        help="Discard the first successful completion response and exercise exact-proof recovery.",
    )

    reactivate = sub.add_parser(
        "reactivate", help="Run stale-installation reactivation and verify credential rotation"
    )
    add_common_arguments(reactivate)
    add_token_arguments(reactivate, alias="--reactivation-token")
    reactivate.add_argument(
        "--no-verify", action="store_true", help="Skip post-reactivation full verification"
    )
    reactivate.add_argument(
        "--simulate-lost-completion-response",
        action="store_true",
        help="Discard the first rotated credential and exercise exact-proof recovery.",
    )

    checkin = sub.add_parser("check-in", help="Perform a real check-in and renew the lease")
    add_common_arguments(checkin)
    checkin.add_argument(
        "--telemetry",
        choices=("configured", "version-only", "none"),
        default="configured",
        help="Lifecycle telemetry: configured version/build, version-only, or neither.",
    )

    refresh = sub.add_parser(
        "refresh", help="Refresh lease then run configuration and conditional sync"
    )
    add_common_arguments(refresh)
    refresh.add_argument(
        "--telemetry",
        choices=("configured", "version-only", "none"),
        default="configured",
        help="Lifecycle telemetry: configured version/build, version-only, or neither.",
    )

    status = sub.add_parser(
        "status", help="Show local fake-device state and server-reported status"
    )
    add_common_arguments(status)

    configuration = sub.add_parser(
        "configuration", help="Retrieve and validate the tablet configuration"
    )
    add_common_arguments(configuration)

    manifest = sub.add_parser(
        "manifest", help="Fetch and verify the signed manifest (optionally download datasets)"
    )
    add_common_arguments(manifest)
    manifest.add_argument(
        "--etag", "--if-none-match", dest="if_none_match", help="Cached manifest ETag"
    )
    manifest.add_argument(
        "--download", action="store_true", help="Also decrypt and validate datasets"
    )

    download = sub.add_parser(
        "download", help="Download, decrypt, and validate publication datasets"
    )
    add_common_arguments(download)
    download.add_argument(
        "--dataset",
        action="append",
        metavar="TYPE",
        help="Restrict to this dataset type. Repeatable. Defaults to all authorized datasets.",
    )

    verify = sub.add_parser(
        "verify", help="Validate the current active tablet API, manifest, and all datasets"
    )
    add_common_arguments(verify)

    update = sub.add_parser(
        "update-check", help="Compare backend publication state to the last verified manifest"
    )
    add_common_arguments(update)
    update.add_argument(
        "--expect-changed",
        action="append",
        default=[],
        metavar="DATASET_TYPE",
        help="Assert this dataset changed since the last verified manifest. Repeatable.",
    )

    signing_key = sub.add_parser(
        "signing-key", help="Fetch or inspect an exact Ed25519 signing-key version"
    )
    add_common_arguments(signing_key)
    signing_key.add_argument(
        "version", help="Exact signing-key version to fetch; never falls back."
    )

    terminal_matrix = sub.add_parser(
        "terminal-matrix",
        help="Verify a REPLACED or REVOKED credential gets status only, never data access",
    )
    add_common_arguments(terminal_matrix)
    terminal_matrix.add_argument(
        "--signing-key-version",
        default="1",
        help="Exact key version to probe (default: 1).",
    )
    update.add_argument(
        "--expect-unchanged",
        action="append",
        default=[],
        metavar="DATASET_TYPE",
        help="Assert this dataset did not change. Repeatable.",
    )
    update.add_argument(
        "--expect-version-increase",
        action="append",
        default=[],
        metavar="DATASET_TYPE",
        help="Assert the dataset's scope-local publication version increased. Repeatable.",
    )

    reset = sub.add_parser(
        "reset", help="Delete only the local fake-iPad state (never server-side state)"
    )
    reset.add_argument(
        "--state-dir",
        default=DEFAULT_STATE_DIR,
        help=f"Persistent fake-iPad test state directory (default: {DEFAULT_STATE_DIR})",
    )
    reset.add_argument(
        "--json", action="store_true", help="Emit a single machine-readable JSON result on stdout"
    )

    return parser


def build_client(
    args: argparse.Namespace, out: Output
) -> tuple[DeviceState, FakeIPadClient | None]:
    state = DeviceState(Path(args.state_dir))
    if args.app_version is not None:
        require_app_version(args.app_version, label="--app-version")
    if args.app_build is not None:
        require_app_build(args.app_build, label="--app-build")
    if args.clear_app_build and args.app_build is not None:
        fail("--clear-app-build cannot be combined with --app-build")
    if args.app_version is not None or args.app_build is not None or args.clear_app_build:
        state.set_app_identity(
            app_version=args.app_version or state.app_version,
            app_build=None
            if args.clear_app_build
            else (args.app_build if args.app_build is not None else state.app_build),
        )
    server_url = args.server or state.server_url
    if not server_url:
        return state, None
    api = ApiClient(
        server_url,
        insecure=args.insecure,
        timeout=args.timeout,
        verbose=args.verbose,
        out=out,
    )
    client = FakeIPadClient(
        state,
        api,
        app_version=state.app_version,
        app_build=state.app_build,
        verbose=args.verbose,
        save_plaintext=args.save_plaintext,
        out=out,
    )
    return state, client


def cmd_status(state: DeviceState, client: FakeIPadClient | None, out: Output) -> dict[str, Any]:
    out.banner("FIREDASH FAKE IPAD — STATUS")
    summary = state.local_summary()
    out.line(f"  Server:                {summary['server_url'] or '(not configured)'}")
    out.line(f"  Installation UUID:     {summary['installation_uuid'] or '(none)'}")
    out.line(f"  Installation ID:       {summary['installation_id'] or '(none)'}")
    out.line(f"  Tablet ID:             {summary['tablet_id'] or '(none)'}")
    out.line(f"  Adopted:               {'yes' if summary['adopted'] else 'no'}")
    out.line(f"  App version:           {summary['app_version']}")
    out.line(f"  App build:             {summary['app_build'] or '(not configured)'}")
    out.line(f"  Credential present:    {'yes' if summary['credential_present'] else 'no'}")
    out.line(f"  Lease valid until:     {summary['authorization_valid_until'] or '(n/a)'}")
    out.line(f"  Last server time:       {summary['server_time'] or '(n/a)'}")

    result: dict[str, Any] = dict(summary)
    if state.has_credential and client is not None:
        try:
            result["server"] = client.get_status()
        except ClientError as exc:
            out.line(f"  server status:         unavailable ({exc})")
            result["server_error"] = str(exc)
    elif not state.has_credential:
        out.line("  server status:         not adopted; run 'adopt' first")
    else:
        out.line("  server status:         no server configured; pass --server")
    out.banner("STATUS RESULT: PASS")
    return result


def _require_client(client: FakeIPadClient | None) -> FakeIPadClient:
    if client is None:
        fail("--server is required for the first run (or when state has no stored server)")
    return client


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = Output(json_mode=args.json)

    try:
        if args.command == "reset":
            state = DeviceState(Path(args.state_dir))
            out.banner("FIREDASH FAKE IPAD — RESET (LOCAL STATE ONLY)")
            removed = state.reset()
            out.line(
                f"Removed local fake-iPad state only. Files deleted: {len(removed)}. "
                "No server-side change was made."
            )
            result: dict[str, Any] = {"ok": True, "command": "reset", "removed": removed}
            out.emit(result)
            return 0

        if args.command in ("adopt", "provision", "reactivate"):
            apply_provisioning_payload(args)
        state, client = build_client(args, out)

        if args.command in ("adopt", "provision"):
            token = get_token(args, prompt="Adoption token (hidden): ")
            data = _require_client(client).adopt(
                token,
                verify=not args.no_verify,
                simulate_lost_completion_response=args.simulate_lost_completion_response,
            )
            result = {"ok": True, "command": "adopt", **data}

        elif args.command == "reactivate":
            token = get_token(args, prompt="Reactivation token (hidden): ")
            data = _require_client(client).reactivate(
                token,
                verify=not args.no_verify,
                simulate_lost_completion_response=args.simulate_lost_completion_response,
            )
            result = {"ok": True, "command": "reactivate", **data}

        elif args.command == "check-in":
            data = _require_client(client).check_in(telemetry=args.telemetry)
            result = {"ok": True, "command": "check-in", **data}

        elif args.command == "refresh":
            data = _require_client(client).refresh(telemetry=args.telemetry)
            result = {"ok": True, "command": "refresh", **data}

        elif args.command == "status":
            data = cmd_status(state, client, out)
            result = {"ok": True, "command": "status", **data}

        elif args.command == "configuration":
            data = _require_client(client).get_configuration()
            result = {"ok": True, "command": "configuration", **data}

        elif args.command == "manifest":
            data = _require_client(client).manifest(
                if_none_match=args.if_none_match, download=args.download
            )
            result = {"ok": True, "command": "manifest", **data}

        elif args.command == "download":
            data = _require_client(client).download(dataset_types=args.dataset)
            result = {"ok": True, "command": "download", **data}

        elif args.command == "verify":
            data = _require_client(client).verify()
            result = {"ok": True, "command": "verify", **data}

        elif args.command == "update-check":
            data = _require_client(client).update_check(
                expect_changed=args.expect_changed,
                expect_unchanged=args.expect_unchanged,
                expect_version_increase=args.expect_version_increase,
            )
            result = {"ok": True, "command": "update-check", **data}

        elif args.command == "signing-key":
            data = _require_client(client).inspect_signing_key(args.version)
            out.banner("SIGNING KEY RESULT: PASS")
            out.line(f"  requested version: {data['requested_version']}")
            out.line(f"  algorithm:         {data['algorithm']}")
            out.line(f"  SHA-256:           {data['public_key_sha256']}")
            out.line(f"  source:            {data['source']}")
            result = {"ok": True, "command": "signing-key", **data}

        elif args.command == "terminal-matrix":
            data = _require_client(client).terminal_endpoint_matrix(
                signing_key_version=args.signing_key_version
            )
            out.banner("TERMINAL ENDPOINT MATRIX: PASS")
            result = {"ok": True, "command": "terminal-matrix", **data}

        else:
            parser.error(f"Unknown command: {args.command}")

        out.emit(result)
        return 0

    except KeyboardInterrupt:
        out.line("\nInterrupted.")
        return 130
    except ClientError as exc:
        out.banner("RESULT: FAIL")
        out.line(str(exc))
        out.emit({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
