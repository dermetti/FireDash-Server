"""HTTP transport for the FireDash tablet API.

No Django here: plain ``urllib`` with an explicit TLS context and a finite
timeout on every request. TLS verification is on by default; ``--insecure`` is
an explicit, warned-about lab-only override.
"""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request

from tools.fake_ipad.errors import fail
from tools.fake_ipad.output import Output, pretty, redact, text_indent


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str

    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.body.decode("utf-8"))
        except Exception as exc:
            fail(f"{self.url}: response is not valid UTF-8 JSON: {exc}")
        if not isinstance(value, dict):
            fail(f"{self.url}: expected JSON object")
        return value


@dataclass(frozen=True)
class Problem:
    status: int
    code: str | None
    detail: str | None
    request_id: str | None
    minimum_app_version: str | None


def parse_problem(response: HttpResponse) -> Problem:
    """Parse a problem response without treating display text as protocol state."""
    body: dict[str, Any] = {}
    if response.body:
        try:
            body = response.json()
        except Exception:
            pass  # Diagnostics below retain the HTTP status even for malformed bodies.
    return Problem(
        status=response.status,
        code=body.get("code") if isinstance(body.get("code"), str) else None,
        detail=body.get("detail") if isinstance(body.get("detail"), str) else None,
        request_id=body.get("request_id") if isinstance(body.get("request_id"), str) else None,
        minimum_app_version=(
            body.get("minimum_app_version")
            if isinstance(body.get("minimum_app_version"), str)
            else None
        ),
    )


def problem_text(response: HttpResponse) -> str:
    """Return a safe, redacted one-line description of a failed response."""
    problem = parse_problem(response)
    if problem.code:
        request_id = f", request_id={problem.request_id}" if problem.request_id else ""
        detail = f": {problem.detail}" if problem.detail else ""
        return f"HTTP {problem.status} code={problem.code}{request_id}{detail}"
    content_type = response.headers.get("content-type", "")
    if "json" in content_type and response.body:
        try:
            return json.dumps(redact(response.json()), sort_keys=True, ensure_ascii=False)
        except Exception:
            pass  # nosec B110
    return f"HTTP {response.status} ({len(response.body)} bytes)"


class ApiClient:
    def __init__(
        self,
        server_url: str,
        *,
        insecure: bool = False,
        timeout: float = 30.0,
        verbose: bool = False,
        out: Output | None = None,
    ) -> None:
        self.out = out or Output()
        parsed = parse.urlsplit(server_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            fail("--server must start with https:// or http://")
        if (
            parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            fail("--server must be an origin, without credentials, path, query, or fragment")
        self.server_url = server_url.rstrip("/")
        self._origin = self._origin_tuple(self.server_url)
        if parsed.scheme == "http" and not insecure:
            fail(
                "Refusing plaintext HTTP. Use HTTPS, or explicitly pass --insecure "
                "for a lab-only test."
            )
        self.timeout = timeout
        self.verbose = verbose
        if insecure:
            self.out.line(
                "WARNING: TLS certificate verification is DISABLED (--insecure). "
                "This is for lab-only testing."
            )
        # --insecure is an explicit, warned-about lab-only override.
        self.ssl_context = (
            ssl._create_unverified_context() if insecure else ssl.create_default_context()  # nosec B323
        )
        self._opener = request.build_opener(
            _NoRedirect(), request.HTTPSHandler(context=self.ssl_context)
        )

    @staticmethod
    def _origin_tuple(url: str) -> tuple[str, str, int]:
        parsed = parse.urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            fail(f"Invalid HTTP origin: {url!r}")
        try:
            port = parsed.port
        except ValueError:
            fail(f"Invalid HTTP port in origin: {url!r}")
        return (
            parsed.scheme.lower(),
            parsed.hostname.lower(),
            port if port is not None else (443 if parsed.scheme.lower() == "https" else 80),
        )

    def make_url(self, path: str) -> str:
        if path.startswith(("https://", "http://")):
            if self._origin_tuple(path) != self._origin:
                fail(f"Server returned a URL outside the API origin: {path}")
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.server_url + path

    def call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        bearer: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        url = self.make_url(path)
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "FireDash-Fake-iPad/2.0",
        }
        if bearer:
            request_headers["Authorization"] = f"Bearer {bearer}"
        if headers:
            request_headers.update(headers)

        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        self.out.line(f"\n→ {method} {url}")
        if self.verbose and json_body is not None:
            self.out.line("  request JSON:")
            self.out.line(text_indent(pretty(json_body), "    "))

        req = request.Request(url, data=body, method=method, headers=request_headers)

        try:
            # Only the validated --server origin is ever opened.
            with self._opener.open(req, timeout=self.timeout) as resp:  # nosec B310
                response = HttpResponse(
                    status=resp.status,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    body=resp.read(),
                    url=url,
                )
        except error.HTTPError as exc:
            response = HttpResponse(
                status=exc.code,
                headers={k.lower(): v for k, v in exc.headers.items()},
                body=exc.read(),
                url=url,
            )
        except error.URLError as exc:
            fail(f"{method} {url}: transport failure: {exc}")
        except TimeoutError as exc:
            fail(f"{method} {url}: timeout: {exc}")

        self.out.line(f"← HTTP {response.status}")
        request_id = response.headers.get("x-request-id")
        if request_id:
            self.out.line(f"  X-Request-ID: {request_id}")
        if self.verbose and response.body:
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                try:
                    self.out.line("  response JSON:")
                    self.out.line(text_indent(pretty(response.json()), "    "))
                except Exception:
                    # Verbose output only; never fatal.
                    pass  # nosec B110
            else:
                self.out.line(f"  response body: {len(response.body)} bytes")

        return response

    def expect(self, response: HttpResponse, *ok: int, label: str) -> HttpResponse:
        if response.status not in ok:
            fail(f"{label}: {problem_text(response)}")
        return response


class _NoRedirect(request.HTTPRedirectHandler):
    """Reject redirects so credentials can never cross an origin boundary."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> request.Request | None:
        return None
