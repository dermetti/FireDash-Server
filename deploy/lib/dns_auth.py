"""Small Certbot DNS adapters; no plugin imports or provider API implementations.

Existing plugins are privileged dependencies explicitly approved by the operator.
The multi adapter uses a pinned bridge and a deliberately bounded provider set.
"""

import re
from pathlib import Path

MULTI_VERSION = "5.6.0"
CERTBOT_VERSION = "5.7.0"
# Verified against lego v5.6.0 bundled by certbot-dns-multi 5.6.0. Adding
# mechanisms requires reviewing their options, not merely copying lego's list.
PROVIDERS = frozenset(
    {"cloudflare", "netcup", "hetzner", "ovh", "digitalocean", "desec", "gandiv5", "porkbun"}
)
KEYS = (
    "FIREDASH_TLS_DNS_BACKEND",
    "FIREDASH_TLS_DNS_PROVIDER",
    "FIREDASH_TLS_DNS_AUTHENTICATOR",
    "FIREDASH_TLS_DNS_PLUGIN_APPROVED",
    "FIREDASH_TLS_DNS_PROPAGATION_SECONDS",
)


def backend(values):
    return values.get("FIREDASH_TLS_DNS_BACKEND") or "legacy-netcup"


def authenticator(values):
    kind = backend(values)
    if kind == "certbot-multi":
        return "dns-multi"
    if kind == "legacy-netcup":
        return "dns-netcup"
    return values.get("FIREDASH_TLS_DNS_AUTHENTICATOR", "")


def validate(values, require):
    kind = backend(values)
    require(
        kind in {"certbot-multi", "certbot-existing", "legacy-netcup"}, "unsupported DNS backend"
    )
    if kind != "legacy-netcup":
        require(
            values.get("FIREDASH_TLS_DNS_PLUGIN_APPROVED") == "yes",
            "explicit privileged DNS plugin/provenance approval required",
        )
    auth = authenticator(values)
    require(
        bool(re.fullmatch(r"dns-[a-z][a-z0-9-]{0,48}", auth))
        and auth not in {"dns-manual", "dns-exec", "dns-external", "dns-httpreq"},
        "unsupported DNS authenticator",
    )
    if kind == "certbot-existing":
        require(
            values.get("FIREDASH_TLS_ADOPT_DNS01") == "yes",
            "existing backend requires explicit DNS lineage adoption",
        )
    if kind == "certbot-multi":
        require(
            values.get("FIREDASH_TLS_ADOPT_DNS01") != "yes",
            "use existing backend to adopt a dns-multi lineage",
        )
    provider = values.get("FIREDASH_TLS_DNS_PROVIDER", "")
    if auth == "dns-multi":
        require(
            provider in PROVIDERS, "DNS provider unsupported or requires a separate security review"
        )
    else:
        require(not provider, "provider selector applies only to dns-multi")
    seconds = values.get("FIREDASH_TLS_DNS_PROPAGATION_SECONDS", "900")
    require(
        bool(re.fullmatch(r"[0-9]{1,4}", seconds)) and 30 <= int(seconds) <= 1800,
        "DNS propagation seconds must be 30 through 1800",
    )


def credential_parameter(values):
    return authenticator(values).replace("-", "_") + "_credentials"


def credentials(values, params, protect, require):
    key = credential_parameter(values)
    path = Path(params.get(key, ""))
    protect(path)
    expected = values.get("FIREDASH_TLS_DNS_CREDENTIALS")
    require(
        not expected or str(path) == expected,
        "DNS credential path differs from approved configuration",
    )
    # No ambient credential discovery or unreviewed auxiliary credential files.
    require(
        not any(k.endswith("_credentials") and k != key and v for k, v in params.items()),
        "additional DNS credential mechanisms require review",
    )
    if authenticator(values) == "dns-multi":
        multi_credentials(path, values, require)
    return path


def multi_credentials(path, values, require):
    # Parse literal INI assignments only. Never evaluate or print secret values.
    # lego exposes _FILE references and process-wide environment controls: those
    # need a separate review and are deliberately excluded here.
    items = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        require(
            "=" in line and not line.startswith("["),
            "DNS credentials must contain literal assignments",
        )
        key, value = (part.strip() for part in line.split("=", 1))
        require(key not in items, "duplicate DNS credential setting")
        require(
            key == "dns_multi_provider" or bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", key)),
            "invalid DNS credential option",
        )
        provider = values["FIREDASH_TLS_DNS_PROVIDER"]
        prefixes = {"gandiv5": "GANDIV5", "digitalocean": "DO"}
        prefix = prefixes.get(provider, provider.upper()) + "_"
        require(
            key == "dns_multi_provider" or key.startswith(prefix) and not key.endswith("_FILE"),
            "DNS credential option outside approved provider; file/command indirection prohibited",
        )
        items[key] = value
    require(
        items.get("dns_multi_provider") == values["FIREDASH_TLS_DNS_PROVIDER"],
        "DNS credential provider differs from approved provider",
    )
    require(len(items) > 1, "DNS provider credentials missing")


def check_plugin(values, args, run, require, *, protect=None):
    validate(values, require)
    output = run(args + ["plugins", "--authenticators", "--text"]).decode()
    # Exact token match, not substring (dns-netcup-evil must not satisfy netcup).
    require(
        authenticator(values) in re.findall(r"(?m)^\s*(?:\*\s+)?(dns-[a-z0-9-]+)\s*$", output),
        "configured DNS plugin missing in selected Certbot environment",
    )
    if authenticator(values) == "dns-multi":
        # Metadata only, using a fixed program; never import a selected plugin.
        python = Path(args[0]).with_name("python")
        require(
            python.is_file(), "dns-multi requires an isolated reviewed Certbot virtual environment"
        )
        if protect is not None:
            protect(python)
        script = (
            "from importlib.metadata import version; print(version('certbot')); "
            "print(version('certbot-dns-multi'))"
        )
        versions = run([str(python), "-I", "-c", script]).decode().splitlines()
        require(
            versions == [CERTBOT_VERSION, MULTI_VERSION],
            "Certbot/dns-multi version mismatch; install the reviewed dependency lock",
        )


def issuance_args(values, protect, require):
    require(
        backend(values) == "certbot-multi",
        "new DNS issuance requires certbot-multi; legacy/existing backends are renewal-only",
    )
    path = Path(values.get("FIREDASH_TLS_DNS_CREDENTIALS", ""))
    protect(path)
    multi_credentials(path, values, require)
    return [
        "--authenticator",
        "dns-multi",
        "--dns-multi-credentials",
        str(path),
        "--dns-multi-propagation-seconds",
        values.get("FIREDASH_TLS_DNS_PROPAGATION_SECONDS", "900"),
    ]
