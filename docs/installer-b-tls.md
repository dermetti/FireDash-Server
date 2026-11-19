# Installer B: certificate lifecycle

This is an operations change. It adds no database migrations, application
privileges, key selectors or publication/Report Transfer cryptographic changes.
Local mock results do not establish real ACME or systemd acceptance.

## Modes and ownership

| Mode | Issuance and renewal | Existing paths |
| --- | --- | --- |
| `manual` | External owner; installer validates only | Preserved; `--tls-cert` and `--tls-key` remain supported |
| `letsencrypt-http01` | Default on a fresh public VPS; Certbot webroot | FireDash verified copies under `/etc/fire-backend/tls/current/` |
| `letsencrypt-dns01` | Netcup DNS API; no inbound ACME access required | Fresh issuance uses verified copies; explicit adoption keeps existing Certbot paths |

An installation without a persisted mode remains `manual`. No DNS-01 lineage is
converted to HTTP-01. The supported mode transition is explicit adoption from
manual to DNS-01 with `--tls-adopt-dns01`; other transitions fail for operator
review. Do not edit the mode to bypass validation. Renewals never run Django,
Gunicorn, migrations or database commands.

## Fresh public Debian 13 amd64 VPS

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

## Existing CGNAT Proxmox installation: retain DNS-Netcup

`firedash.mjblab.de` stays DNS-01. No public IP, port forwarding or inbound port
80/443 is required for ACME issuance or renewal. Application access still needs
the installation's existing HTTPS network path; this change does not create a
tunnel or expose the application.

After release review and separate deployment authorization, the intended
explicit adoption command is:

```sh
sudo bash /srv/firedash/current/deploy/install.sh --ref <approved-installer-b-sha> \
  --tls-mode letsencrypt-dns01 --tls-adopt-dns01 \
  --tls-certbot /usr/local/bin/certbot --tls-lineage firedash.mjblab.de \
  --tls-cert /etc/letsencrypt/live/firedash.mjblab.de/fullchain.pem \
  --tls-key /etc/letsencrypt/live/firedash.mjblab.de/privkey.pem
```

The command is documentation, not evidence of execution. The existing release
bootstrap fetches the selected candidate before processing the new flags.
Adoption checks the named lineage's `dns-netcup` authenticator, production ACME
server, absence of foreign installers/hooks, plugin availability, and protected
credential metadata. A healthy certificate is validated without issuing or
renewing it. The original account, certificate, key, credential bytes and
renewal INI are not rewritten by adoption. Subsequent installer runs load the
persisted DNS mode and converge without changing its authenticator or paths.

The named Certbot executable must already contain the working DNS-Netcup plugin;
the installer never reinstalls or upgrades that environment during adoption.
The renewal INI must identify the existing credential file. That file must be
regular `root:root 0600`, under real root-owned directories without group/other
writes. Diagnostics never print credential contents. Existing unusual hooks,
installers or permissions require review rather than automatic alteration.

Before adoption, resolve competing host Certbot/ACME timers or cron jobs
explicitly. The installer reports conflicts and never disables unrelated jobs.
Standard disabled/static Certbot units are harmless; the packaged Debian cron
entry guarded against systemd is dormant. A host with other certificates may
need an operator-reviewed scheduler split: FireDash owns only the selected
lineage, and must not interrupt renewal of other certificates. Keep their
backups and schedules intact. Root-run ad hoc Certbot commands must also be
coordinated with the FireDash lock and Certbot's own namespace locks.

## Fresh DNS-Netcup installation

Use `letsencrypt-dns01` without the adoption flag. Install a reviewed Certbot
environment with the [DNS-Netcup plugin](https://github.com/coldfix/certbot-dns-netcup)
first; it must be separate from the FireDash application virtualenv. Provision
the Netcup credential INI privately as root, with mode 0600 and protected parent
directories. Use the plugin's documented format without putting its values in
shell arguments or application configuration. Then supply:

```sh
sudo bash deploy/install.sh --ref <approved-sha> \
  --base-url https://firedash.example.org --tls-mode letsencrypt-dns01 \
  --tls-certbot /opt/firedash-certbot/bin/certbot \
  --dns-netcup-credentials /etc/private-acme/netcup.ini \
  --acme-email operator@example.org --acme-agree-tos
```

Fresh DNS-01 uses a private FireDash Certbot namespace and a 900-second DNS
propagation wait. It makes no public-address or inbound HTTP probe. DNS API and
ACME outbound access must work. The plugin/environment and credential file are
operator prerequisites; the installer does not request or generate API keys.

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

Back up private certificate, archive, renewal, ACME account and DNS credential
state alongside protected installer configuration, with encryption and access
restricted to operators. Include `/etc/fire-backend/tls` and its recovery journal
for adopted installations. Never copy credentials into Django environment
files, CI variables, tablets, reports or tickets. A restore onto a different
host needs private ownership/mode checks, the same plugin environment and
lineage paths, scheduler conflict review, and validated HTTPS before traffic
moves. HTTP-01 additionally needs corrected A/AAAA records and open ports;
DNS-01 still needs no inbound ACME access. Do not regenerate signing keys or
modify existing cryptographic selectors during restore.

## Required release evidence

Focused fixtures use synthetic certificates and mocked Certbot/DNS commands.
Native Linux tests additionally exercise real Nginx location routing and
transactional symlink rollback when available. Neither proves real ACME.
After review and separately authorized commit/push, run the full GitHub CI
suite, including native deploy tests. In a disposable Debian 13/systemd
environment, test both fresh HTTP-01 and fresh/adopted DNS-01 with Let's Encrypt
**staging**, disposable accounts/domains/API credentials and separate filesystem
roots; never test against the live Netcup credential file or production issuer.
Record unattended timer execution, an actual staging renewal, failure/retry,
installer concurrency, unchanged application state, key/credential preservation
and native service sandbox behavior. Only a separately authorized deployment
may validate the existing Proxmox installation; none is part of local development.
