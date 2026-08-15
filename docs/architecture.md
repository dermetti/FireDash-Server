# Architecture

## Product and tenancy boundary

FireDash Server provisions reference data for fire departments and their
stations. It deliberately has no endpoint or storage model for incident,
intervention, or generic tablet-upload data. Department and station scope are
enforced in application queries, permissions, and database constraints.

The primary domain records are departments, stations, vehicles, personnel,
historical assignments, tablets and installations, hydrants, accepted fire
plans, dataset publications, and append-only audit events.

## Platform

The server uses Django 5.2, Django REST Framework, PostgreSQL 17 with PostGIS,
Gunicorn behind Nginx, and hardened systemd units on Debian LXC. PostgreSQL is
the authoritative work queue and state store. FireDash does not use Redis,
Celery, Docker, Kubernetes, or a separate queue broker.

## Assignments

Assignments preserve history. `valid_from` is inclusive and `valid_until` is
exclusive, so an assignment is effective at time *t* when
`valid_from <= t < valid_until`; an unset end is open-ended. Ended rows are
historical rather than deleted. A person has exactly one current HOME
assignment; transfers close the prior HOME assignment before opening the new
one. Temporary assignments remain distinct, expire by their effective end,
and do not replace the HOME assignment.

## Publications

Publication work is split into three operational lanes:

1. **Delivery** is a persistent worker that polls at roughly two seconds and
   generates `DatasetKeyGrant` and `SignedManifest` records only.
2. **Build** processes dataset builds only. Source changes coalesce into one
   queued intent per scope for the nightly 00:05 build timer. An authorised
   Build & publish action can promote that same intent and wake the build
   service through a narrow Unix socket.
3. **Maintenance** performs credential-free retention and artifact cleanup.

`source_revision` tracks internal source changes. A queued intent always means
“build the latest source for this scope”; changes while a build is running
coalesce into one follow-up intent. A successful build is auto-published only
when its source snapshot is still current. Failed or obsolete work leaves the
known-good publication active.

Each build attempt receives one immutable, monotonically increasing version
within its dataset scope. The version is cryptographically bound to that
attempt, including failed and obsolete attempts, so successful/current
versions may contain gaps. Scheduled, manual, and bulk-expedited origins are
recorded separately. Rollback is an explicit recovery action to a known-good
historical publication.

Current registered dataset types are `department_hydrants`,
`department_fire_plans`, and `station_personnel`.

## Tablet cryptography

Each publication has one encrypted artifact. A random AES-256-GCM content
encryption key (CEK) encrypts it; the CEK is protected by the publication KEK.
Each authorised installation receives an HPKE-wrapped CEK grant. Ed25519
signatures authenticate artifacts and manifests.

Gunicorn authorises tablet requests but does not receive the publication KEK
or Ed25519 private signing key. Those credentials are supplied only to the
`fire_publication` worker boundary. The web service has the signing public
key needed to publish verification information. The detailed tablet contract
is in [tablet-api.md](tablet-api.md); security rationale is in
[security.md](security.md).
