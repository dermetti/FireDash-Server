# Installer B.1: provider-independent certificate lifecycle

This is an operations change. It adds no database migrations, application
privileges, key selectors or publication/Report Transfer cryptographic changes.
Local mock results do not establish real ACME or systemd acceptance.

## Modes and ownership

| Mode | Issuance and renewal | Existing paths |
| --- | --- | --- |
| `manual` | External owner; installer validates only | Preserved; `--tls-cert` and `--tls-key` remain supported |
| `letsencrypt-http01` | Default on a fresh public VPS; Certbot webroot | FireDash verified copies under `/etc/fire-backend/tls/current/` |
| `letsencrypt-dns01` | Reviewed Certbot DNS adapter; no inbound ACME access required | Fresh issuance uses verified copies; explicit adoption keeps existing Certbot paths |

An installation without a persisted mode remains `manual`. No DNS-01 lineage is
converted to HTTP-01. The supported mode transition is explicit adoption from
manual to DNS-01 with `--tls-adopt-dns01`; other transitions fail for operator
review. Do not edit the mode to bypass validation. Renewals never run Django,
Gunicorn, migrations or database commands.

## A. Fresh public Debian 13 amd64 VPS

Point every published A and AAAA record at a public interface on this server.
Remove stale AAAA records or configure IPv6 routing and firewall rules correctly.
The preflight requires directly assigned public addresses; reverse proxies,
shared hosting and public-to-private NAT need a separately reviewed network plan
or DNS-01. Allow public inbound TCP 80 and 443 through the VPS and provider
firewalls. Outbound HTTPS and working DNS resolution are also required.

Run the normal exact-SHA installer entrypoint. A fresh installation defaults to
HTTP-01 and prompts for the origin, ACME contact email and explicit acceptance
of the applicable [Let's Encrypt subscriber agreement](https://letsencrypt.org/repository/).
For repeatable noninteractive configuration:

```sh
sudo bash deploy/install.sh --ref <approved-sha> \
  --base-url https://firedash.example.org --tls-mode letsencrypt-http01 \
  --acme-email operator@example.org --acme-agree-tos
```

Missing inputs still use the protected terminal prompt; supply initial admin
options as documented for a completely unattended first install. The installer
installs Debian Certbot/Nginx prerequisites when absent. It validates hostname
and all locally resolved A/AAAA addresses, installs only the FireDash HTTP
bootstrap site, and probes a random challenge through each address. These local
probes cannot prove Internet reachability; the ACME validation is authoritative.
Ordinary bootstrap requests receive 503. Certbot uses HTTP-01 webroot, without a
Nginx installer plugin. Only after certificate/key/hostname/date validation does
the installer activate its final HTTPS site and proceed to application setup.

The challenge location remains reachable over HTTP, without redirection, during
renewal. The ordinary redirect is inside `location /`, since a server-level
`return` would run before challenge location selection. Protected publication
locations, bearer-log suppression and unrelated sites are preserved.

## DNS backends and privileged dependencies

Certificate mode and DNS backend are separate. `certbot-multi` is the supported
fresh DNS backend. `certbot-existing` adopts a named existing Certbot lineage
without changing its authenticator. There is no new native-plugin issuance
backend; compatible native plugins can be adopted. Both reuse the same timer,
shared installer lock, scoped renewal, validation, deployment and rollback.

The reviewed candidate is **Certbot/acme 5.7.0 + certbot-dns-multi 5.6.0**, whose
bridge bundles lego v5.6.0. Python package metadata permits this combination;
native Debian installation and actual staging remain acceptance gates.
The complete transitive, SHA256-pinned lock is
[deploy/acme/requirements.lock](../deploy/acme/requirements.lock).
Provider availability is checked against that pinned bridge and a bounded
FireDash registry: `cloudflare`, `netcup`, `hetzner`, `ovh`, `digitalocean`,
`desec`, `gandiv5`, `porkbun`. This is **availability, not end-to-end acceptance**;
none has completed B.1 live staging acceptance here. Other providers fail closed
until their mechanisms/options are reviewed and added to the registry.

Upstream interfaces and provenance:
[certbot-dns-multi 5.6.0](https://github.com/alexzorin/certbot-dns-multi/tree/5.6.0),
[plugin source](https://github.com/alexzorin/certbot-dns-multi/blob/5.6.0/certbot_dns_multi/_internal/dns_multi.py),
[bridge dependencies](https://github.com/alexzorin/certbot-dns-multi/blob/5.6.0/certbot_dns_multi/_internal/bridge/go.mod),
[lego provider documentation](https://go-acme.github.io/lego/dns/).
The live lego website may describe providers newer than the pinned bridge.

### Explicit dependency provisioning on Debian 13 amd64

Review the plugin, bridge, lock, package provenance and advisories before root
execution. Review **all plugins in the selected Certbot environment**, since
Certbot can discover/import other installed plugins too. Do not install into the
Django virtualenv, use system Python pip, or replace an established Certbot
installation. The installer never automatically installs a DNS plugin.

On a disposable matching Debian 13/Python 3.13 build host, fetch wheels without
executing source builds:

```sh
python3 -m venv /tmp/firedash-acme-download
/tmp/firedash-acme-download/bin/python -m pip download \
  --only-binary=:all: --require-hashes -r deploy/acme/requirements.lock \
  -d /tmp/firedash-acme-wheels
```

After artifact review, transfer the wheelhouse securely to the new host.
Install Debian's `python3-venv`, `ca-certificates` and `util-linux`, then explicitly
run `sudo bash deploy/acme/install-environment.sh /path/to/reviewed-wheelhouse`.
It takes the FireDash lock, installs wheels offline with hash enforcement into
`/opt/fire-backend-acme-5.7.0-multi-5.6.0`, and activates
`/opt/fire-backend-acme` only after `pip check`. Existing environments are refused,
not overwritten. An interrupted install leaves an unactivated protected directory
for operator inspection. This is an explicitly reviewed installation step, not
an implicit root dependency update during application upgrade.

To update: review upstream changes/advisories, resolve a replacement hash lock
on the target platform (the current lock was generated with `uv pip compile
--python-version 3.13 --python-platform x86_64-unknown-linux-gnu --generate-hashes`),
review registry/options, update the version checks and versioned installation
path, and complete disposable native/staging tests. Do not run `pip install -U`
in a production environment or silently change an established authenticator.

## B. Fresh private/CGNAT installation with dns-multi

Use a domain you control; no public-IP comparison, inbound port or port forwarding
is required for ACME. DNS API, ACME HTTPS and DNS resolution must work outbound.
HTTPS access by users still requires the installation's own network plan.

Create a root:root 0600 **regular file** under protected, root-owned directories,
for example `/etc/private-acme/dns.ini` (parent mode 0700). Use
`dns_multi_provider = cloudflare` and the plugin's documented Cloudflare credential
keys, such as `CLOUDFLARE_DNS_API_TOKEN`, inside that file. For Netcup use
`dns_multi_provider = netcup` and the documented `NETCUP_CUSTOMER_NUMBER`,
`NETCUP_API_KEY`, `NETCUP_API_PASSWORD` keys. Supply actual secret values only
through protected file editing/provisioning, never CLI arguments or application
configuration. Use least-privilege DNS-zone credentials where supported.

```sh
sudo bash deploy/install.sh --ref <approved-sha> \
  --base-url https://firedash.example.org --tls-mode letsencrypt-dns01 \
  --tls-dns-backend certbot-multi --tls-dns-provider cloudflare \
  --tls-dns-plugin-approved --tls-certbot /opt/fire-backend-acme/bin/certbot \
  --tls-dns-credentials /etc/private-acme/dns.ini \
  --tls-dns-propagation-seconds 900 \
  --acme-email operator@example.org --acme-agree-tos
```

The approval flag attests that the operator reviewed the selected privileged
plugin environment and provenance. Missing values prompt interactively. New
issuance uses `dns-multi`, `--dns-multi-credentials` and a bounded 30..1800-second
propagation wait (default 900); no raw API values enter process arguments.
Provider selection is read from the protected file and must match configuration.
Privileged lifecycle child commands receive a minimal fixed environment; ambient
DNS credentials, Python/loader overrides and shell startup settings are excluded.
Only literal INI assignments for the selected provider's key prefix are accepted;
external programs, manual/webhook mechanisms, global environment controls and
`_FILE` indirection are excluded. Secret formats and API implementation remain
inside the upstream plugin. FireDash does not implement provider APIs.

## C. Adopt an existing DNS lineage (including Proxmox Netcup)

First review the existing Certbot executable/environment provenance, installed
plugins, exact lineage name, saved authenticator, approved production ACME server,
credential metadata and scheduler ownership. Inspect these privately as root;
do not paste renewal files, accounts or credential contents into logs/tickets.
Compatible plugins must use a protected `<authenticator_with_underscores>_credentials`
file parameter. Ambient credentials, external commands and auxiliary credential
mechanisms require a separate review and fail closed. A second compatible native
plugin, such as `dns-cloudflare`, uses the same adapter. Adopting `dns-multi`
also requires the provider selector and the pinned Certbot/plugin versions above.

For the established CGNAT Proxmox host, retain DNS-Netcup and run only after
separate deployment authorization:

```sh
sudo bash deploy/install.sh --ref <approved-sha> \
  --base-url https://firedash.mjblab.de --tls-mode letsencrypt-dns01 \
  --tls-dns-backend certbot-existing --tls-dns-authenticator dns-netcup \
  --tls-dns-plugin-approved --tls-adopt-dns01 \
  --tls-certbot /usr/local/bin/certbot --tls-lineage firedash.mjblab.de \
  --tls-cert /etc/letsencrypt/live/firedash.mjblab.de/fullchain.pem \
  --tls-key /etc/letsencrypt/live/firedash.mjblab.de/privkey.pem
```

Healthy adoption does not issue or renew, edit the INI, regenerate/move keys,
recreate accounts, modify credential contents, replace Nginx configuration during
TLS preparation, or change running application state. Actual later Certbot
renewals retain normal account/archive/renewal bookkeeping. Near-expiry/expired
managed certificates can renew through the existing recovery path before the
final validity gate. No public address or inbound probe occurs for DNS-01.

Installations already persisted by Installer B without backend keys retain their
Netcup binding and existing private namespace or adopted host lineage. Missing
backend keys authorize **renewal only**, never fresh issuance. Pre-Installer B
missing modes still mean manual. Do not add/change backend, provider,
authenticator, executable, lineage, credential binding or propagation settings
on an established DNS owner to bypass transition review. Manual-to-DNS adoption
still requires the explicit adoption flag. The old `--dns-netcup-credentials`
argument remains a path-only compatibility alias; use `--tls-dns-credentials`.

No competing scheduler is automatically disabled. For adopted host lineages,
active standard Certbot timers, custom ACME timers/cron or unmanaged hooks fail
closed. Hosts with other certificates need an operator-reviewed scheduler split;
FireDash owns only its named lineage and must not interrupt other renewals.
Root-run ad hoc Certbot commands must use the FireDash lock and respect Certbot's
own namespace locks. Certbot installers, unreviewed hooks, server/authenticator
mismatches and additional credential mechanisms are rejected.

## Renewal, monitoring and recovery

`fire-tls-renew.timer` runs twice daily with up to one hour of random delay and
persistent missed-run recovery. Its root-only oneshot service uses the same
`/run/lock/firedash-install.lock` as the installer; a competing run fails safely
and retries on the next timer event. Certbot also locks its namespace. Fresh
managed certificates use `/etc/fire-backend/acme`, separate work/log directories
and the single `firedash` lineage. Adoption uses only the explicitly selected
host lineage with `renew --cert-name`, preserving the stored DNS authenticator.
There is no unrestricted renewal of all host certificates.

A deploy hook validates the renewed pair, hostname, dates and `nginx -t` before
reloading Nginx. It does not restart Gunicorn. No-op renewals do not reload.
Fresh-mode verified copies use an atomic current pointer and retain previous
generations. Adopted DNS-01 keeps standard Certbot live/archive symlinks; a
root-only recovery journal retains their previous targets across renewal and
restores them if validation fails. Certbot's normal renewal bookkeeping may
update its own INI during an actual renewal; FireDash never patches that INI.
Archive certificate/key files and account state are not deleted.

```sh
systemctl is-enabled fire-tls-renew.timer
systemctl is-active fire-tls-renew.timer
systemctl list-timers fire-tls-renew.timer
journalctl -u fire-tls-renew.service --since '7 days ago'
sudo bash /srv/firedash/current/deploy/verify-deployment.sh
```

An authorized operator can test the configured lineage's staging renewal with
`sudo python3 /usr/local/lib/fire-backend/tls.py dry-run`. This takes the shared
lock and uses Certbot's `--dry-run`, without `--run-deploy-hooks`; it does not
activate certificates, reload Nginx, or change application state. It still
contacts external services and, for DNS-01, uses the configured DNS account to
create/remove validation TXT records. Run automated integration exercises only
with disposable staging accounts/credentials, never the live Netcup file.

The verifier checks hostname, matching key, dates, Nginx paths, tool/plugin,
renewal parameters, installed helper/unit content and timer state. Manual mode
explicitly reports external renewal rather than claiming automation. The
warning window defaults to 30 days; configure `--tls-warning-days 1..60` and
rerun the installer. Warnings go to stderr/journal. Monitor this warning and
failed renewal units with the host's external monitoring system; no new alert
delivery service is supplied.

For a failed managed renewal, inspect the journal and private Certbot logs,
correct DNS API/network/HTTP firewall or Nginx configuration as appropriate,
then `sudo systemctl start fire-tls-renew.service`. Do not force renewal in a
retry loop. Expired managed certificates use the same renewal/recovery path
before the installer's final validity gate. Expired manual certificates must be
renewed by their external owner before rerunning. An ACME failure occurs before
application maintenance; it does not stop a healthy application. Interrupted
first issuance leaves at most the controlled HTTP bootstrap and private ACME
state; rerun with the same configuration. Interrupted upgrades follow the
existing maintenance recovery instructions and must not restart old code after
possible migrations.

## Encrypted TLS backup and restore ? beta recovery gate

**Automatic `fire-backup` TLS coverage is not implemented. B.1 recovery acceptance
is beta-blocked until a separately scheduled encrypted TLS snapshot and isolated
restore drill are demonstrated.** The existing backup job covers database,
application storage and application credentials, not the complete ACME state.
It is unchanged; no retention/pruning behavior is broadened or executed here.

Use a dedicated root-operated snapshot with the existing encrypted Restic
repository/password credential authorization. Take the FireDash lock and ensure
no external Certbot process is running. Keep all snapshots encrypted, restrict
repository and restore access to approved operators, and monitor snapshot
completion. Never stage unencrypted TLS archives in application storage or CI.
For each installation, review a literal, narrow source manifest containing:

- `/etc/fire-backend/install.conf` and `/etc/fire-backend/tls` (verified copies,
  previous generations, activation markers, recovery journal).
- Fresh managed mode: `/etc/fire-backend/acme` in full (renewal INI, live/archive,
  accounts and private keys). Work/log directories are diagnostic, not required
  for certificate recovery; do not copy them into broad application backups.
- Adopted mode: only the selected `/etc/letsencrypt/renewal/<lineage>.conf`,
  `/etc/letsencrypt/live/<lineage>` and `/etc/letsencrypt/archive/<lineage>`,
  plus its referenced account directory under `/etc/letsencrypt/accounts`.
  Live symlinks alone are insufficient. Record the exact account path privately.
- The protected DNS credential file referenced by that lineage and any separately
  approved dependencies. Retain its original absolute path, ownership and mode.
- The reviewed dependency lock, approved package provenance/inventory and exact
  release SHA. Rebuild the isolated plugin environment from pinned artifacts.

Use one encrypted snapshot for the reviewed literal paths, for example
`restic backup --tag firedash --tag tls <reviewed-literal-paths>` inside the
existing protected backup authorization context and shared lock. Do not accept
arbitrary paths from an application request. Do not add `/var/spool` or private
report spools. Do not run `forget --prune` as part of this TLS workflow.
Schedule this root-only operation independently only after permission/sandbox
review; merely documenting or listing paths is not proof of backup coverage.

Restore into a private staging root on an isolated replacement host, never over
an active installation. Verify archive targets, account identity, credentials,
root ownership, file permissions, lineage/hostname and plugin versions before
activating. Reconcile old/new schedulers to prevent concurrent operations and
restore to the same absolute paths. Validate certificates/Nginx, run a disposable
staging unattended renewal and verify application/cryptographic selectors remain
unchanged before moving traffic. HTTP-01 needs corrected A/AAAA/firewall rules;
DNS-01 needs outbound API/ACME access, not public inbound ports. No application
key rotation or database restore is implied by a TLS restore.

## Required release evidence

Focused fixtures use synthetic certificates and mocked Certbot/DNS commands.
Native Linux tests additionally exercise real Nginx location routing and
transactional symlink rollback when available. Neither proves real ACME.
After review and separately authorized commit/push, run the full GitHub CI
suite, including native deploy tests. In a disposable Debian 13/systemd
environment, test fresh HTTP-01, fresh dns-multi (Netcup plus another provider when available),
and adopted native/dns-multi DNS-01 with Let's Encrypt
**staging**, disposable accounts/domains/API credentials and separate filesystem
roots; never test against the live Netcup credential file or production issuer.
Record unattended timer execution, an actual staging renewal, failure/retry,
installer concurrency, unchanged application state, key/credential preservation
and native service sandbox behavior. Only a separately authorized deployment
may validate the existing Proxmox installation; none is part of local development.
