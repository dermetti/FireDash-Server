# Product Requirements Document  
## Fire Department Tablet Provisioning Backend — Beta MVP

**Version:** 3.1  
**Status:** Ready for implementation  
**Scope:** Backend and administrative web portal only  
**Target deployment:** Debian LXC on Proxmox  
**Initial scale:** 2–6 tablets  
**Target architecture:** Capable of later supporting multiple departments and thousands of tablets  
**Architecture:** Django modular monolith  
**Server components:** Open-source software only

---

# 1. Product purpose

Build a secure backend that:

- Manages fire departments, stations, vehicles, personnel, administrators, and tablets.
- Allows department administrators to curate and publish reference data for tablets.
- Adopts, validates, reactivates, removes, and revokes tablet app installations.
- Provisions:
  - Department-wide hydrant data
  - Department-wide fire plans
  - Station-specific personnel data
  - Station-specific incident-commander eligibility and verified email addresses
- Encrypts each published dataset once.
- Makes encrypted datasets decryptable only by authorized app installations.
- Prevents one station from receiving personnel belonging only to another station.
- Requires tablets to check in regularly.
- Makes tablet authorization stale after seven days without a successful check-in.
- Never receives or stores incident or intervention data.

The backend is a reference-data provisioning and authorization system. It is not an incident-management backend.

---

# 2. Implementation instructions

Requirements using **MUST** are mandatory.

The implementation MUST:

- Use Django 5.2 LTS and Django REST Framework.
- Use PostgreSQL with PostGIS.
- Use deny-by-default authorization.
- Apply object-, tenant-, station-, role-, and property-level authorization.
- Use scoped domain services for all department-owned data.
- Include automated permission-isolation tests.
- Use versioned HTTP APIs.
- Generate an OpenAPI 3.1 specification.
- Use RFC 9457 problem responses.
- Target OWASP ASVS Level 2.
- Consider the OWASP API Security Top 10.
- Use reviewed cryptographic libraries and standardized algorithms.
- Never implement cryptographic primitives manually.
- Store no incident-related data.
- Include database migrations, tests, deployment configuration, backup scripts, and administrator documentation.
- Run application processes under unprivileged operating-system accounts.
- Enforce security-sensitive restrictions at both application and database level where specified.

Do not add:

- Docker
- Redis
- Celery
- Kubernetes
- MinIO
- A JavaScript SPA framework
- A separate API gateway
- Billing integration
- Public user registration
- Incident-data endpoints
- Generic tablet upload endpoints

---

# 3. Scope

## 3.1 In scope

- Administrator authentication and TOTP MFA
- Company system administrators
- Department administrators
- Station administrators
- Departments
- Stations
- Vehicles
- Personnel
- Personnel lifecycle and retention
- Home-station assignments
- Department-admin-created temporary personnel assignments
- Incident-commander eligibility
- Verified incident-commander email addresses
- Hydrant import and publication
- Fire-plan upload, quarantine, sanitization, and publication
- Station personnel publication
- Automatic generation of draft dataset updates
- Department-admin publication approval
- Tablets and app installations
- Tablet adoption
- Tablet removal and revocation
- Seven-day tablet authorization leases
- Department-admin-controlled stale-tablet reactivation
- HPKE-based per-installation dataset-key delivery
- Encrypted dataset artifacts
- One manifest API for all authorized updates
- Independent package download endpoints
- Append-only audit logging
- Backups
- Health checks

## 3.2 Out of scope

- Tailnet or Headscale creation
- Headscale policy administration
- iOS application implementation
- Incident synchronization
- Incident report storage or transmission
- Tactical map annotations
- Personnel attendance at incidents
- Exposure records
- Remote operating-system erase or lock
- MDM
- Push notifications
- Billing
- Watch or shift scheduling
- Workforce scheduling
- Station-admin tablet transfers
- Station-admin cross-station personnel assignments
- Two-party transfer approval workflows
- Automated Headscale node management
- Public internet exposure
- Incremental per-document fire-plan synchronization

For the beta, fire plans are published as one monolithic encrypted archive per department version.

---

# 4. Deployment context

The backend will run in an unprivileged Debian LXC on Proxmox.

An existing Headscale-based private network will provide connectivity among:

- Administrator devices
- Beta tablets
- The backend LXC

Tailnet creation, Headscale deployment, client enrollment, DERP configuration, and network-policy management are outside this PRD.

The backend may assume:

- Administrator devices can reach it over HTTPS.
- Tablets can reach it over HTTPS when connected.
- The backend is not directly exposed to the public internet.
- Tablets cannot reach PostgreSQL, SSH, Proxmox, or internal file storage.
- Private-network membership does not replace application authentication or authorization.

Conceptual path:

```text
Administrator browser or tablet
        ↓
Existing private network
        ↓
Nginx
        ↓
Gunicorn
        ↓
Django
        ↓
PostgreSQL/PostGIS
```

---

# 5. Technology stack

Use:

- Debian stable
- Python virtual environment
- Django 5.2 LTS
- Django REST Framework
- PostgreSQL
- PostGIS
- Psycopg 3
- Gunicorn
- Nginx
- systemd
- Django templates and forms
- Minimal vanilla JavaScript
- `drf-spectacular`
- `django-otp`
- `cryptography`
- A reviewed RFC 9180-compatible HPKE library
- `qrcode`
- `python-magic`
- `pikepdf` and/or `qpdf`
- `pytest`
- `pytest-django`
- `factory-boy`
- `ruff`
- `mypy`
- `django-stubs`
- `bandit`
- `pip-audit`
- `restic`

Recommended initial LXC allocation:

```text
1–2 vCPUs
2 GB RAM
20 GB system storage
Separate storage for uploaded and generated files
```

---

# 6. Backend data boundary

The backend MAY store:

- Department and station structure
- Vehicles
- Administrator accounts and assignments
- Personnel master data
- Personnel station assignments
- Incident-commander eligibility
- Verified incident-commander email addresses
- Hydrants
- Fire plans
- Tablets and app installations
- Dataset publications
- Encrypted dataset artifacts
- Encrypted content-encryption keys
- Per-installation HPKE key grants
- Audit events

The backend MUST NOT accept or store:

- Incident identifiers
- Incident timestamps
- Incident commanders selected for incidents
- Personnel selected for incidents
- Tactical map annotations
- Unit dispositions
- Personnel attendance
- Exposure records
- Generated reports
- Encrypted incident-report archives
- Incident-report passwords
- Report-delivery confirmations

There MUST be no generic tablet upload endpoint.

---

# 7. Roles and permissions

Roles are independent assignments. Do not implement one mutually exclusive role field on the user.

## 7.1 Infrastructure administrator

This is not an application role.

Infrastructure administrators may technically access:

- Proxmox
- The LXC
- PostgreSQL
- Backups
- Cryptographic server keys
- Application configuration

Infrastructure access must be restricted to designated technical personnel.

The restriction preventing company system administrators from viewing personnel applies to application-level system administrators, not root-level infrastructure administrators.

## 7.2 Company system administrator

May:

- Create departments
- Activate, suspend, or deactivate departments
- Create the first department administrator
- Disable department administrators for support or security purposes
- View department names and account status
- View aggregate service and tablet counts
- View system-level security events

Must not:

- View or search personnel
- View personnel email addresses
- View hydrants
- View fire plans
- Download datasets
- Manage stations or vehicles
- Adopt tablets
- Reactivate stale tablets
- Publish department data
- Access department operational pages
- Use Django superuser access for normal work

System administrators must use a dedicated management surface that does not serialize or expose department operational data.

## 7.3 Department administrator

May operate only within assigned departments.

May:

- Create additional department administrators
- Disable department administrators
- Create and manage stations
- Create and manage vehicles
- Create and manage station administrators
- Assign station administrators to one or more stations
- Hold station-administrator assignments themselves
- Create and manage personnel
- Change personnel home stations
- Create and end temporary personnel assignments
- Offboard and anonymize personnel
- Set incident-commander eligibility
- Enter and verify incident-commander email addresses
- Create tablets
- Perform initial tablet adoption
- Remove or revoke tablets
- Reactivate stale tablets
- Curate hydrants
- Curate fire plans
- Review automatically generated dataset drafts
- Publish tablet updates
- Roll back dataset publications
- Review department audit events

A department administrator has effective administrative visibility across all stations in that department.

## 7.4 Station administrator

May operate only within explicitly assigned stations.

May:

- View personnel belonging to assigned stations
- Create personnel whose home station is an assigned station
- Edit personnel whose home station is an assigned station
- Activate or deactivate personnel belonging to assigned stations
- Set commander eligibility for manageable personnel
- Update commander email addresses for manageable personnel
- View vehicles and tablets within assigned stations
- View unpublished personnel changes for assigned stations

Must not:

- View another station’s personnel
- Search department-wide personnel
- Create department or station administrators
- Create or delete stations
- Perform initial tablet adoption
- Reactivate stale tablets
- Transfer adopted tablets
- Change personnel home stations
- Create cross-station temporary assignments
- Publish tablet datasets
- Curate department-wide hydrants or fire plans
- Access another department

Only department administrators publish data to tablets.

---

# 8. Future transfer compatibility

Two-party station-admin transfer workflows are not implemented in the MVP.

The design MUST allow later implementation without replacing the core assignment models or authorization services.

## 8.1 Future tablet transfer

Future workflow:

```text
Station A administrator initiates transfer
→ selects a Station B vehicle
→ Station B administrator acknowledges
→ transfer becomes active
```

A department administrator will be able to perform the transfer directly without destination approval.

For the MVP:

- Station-admin tablet transfers are not implemented.
- Tablet station changes require:
  1. Removing the current adoption
  2. Ending the current vehicle assignment
  3. Creating a new vehicle assignment
  4. Performing a new adoption

## 8.2 Future temporary personnel assignment

Future workflow:

```text
Station A administrator initiates temporary assignment
→ selects Station B
→ Station B administrator acknowledges
→ temporary assignment becomes active
```

A department administrator will be able to create the assignment directly.

Home-station changes will always require a department administrator.

For the MVP:

- Department administrators may create temporary assignments directly.
- Station administrators cannot create cross-station temporary assignments.
- Station administrators cannot change home stations.

## 8.3 Design requirements

- Use historical assignment models.
- Do not store only a mutable current station or vehicle field.
- Assignment changes must use domain services.
- Views and serializers must not modify assignment rows directly.
- Future approval workflows must call the same domain services used by department administrators.
- Do not implement unused transfer-request models in the MVP.

---

# 9. Administrator authentication

Administrator accounts MUST use:

- Unique individual accounts
- Django password hashing
- TOTP MFA
- Secure session cookies
- CSRF protection
- Login throttling
- Session expiration
- Reauthentication for sensitive actions

Shared administrator accounts are prohibited.

Sensitive actions include:

- Creating or disabling administrators
- Changing role assignments
- Publishing data
- Generating adoption codes
- Removing tablets
- Reactivating tablets
- Verifying commander email addresses
- Deactivating departments
- Anonymizing or deleting personnel

Initial account setup may use a one-time setup URL displayed once to the creating administrator.

Setup links MUST:

- Contain at least 128 bits of secure randomness
- Be stored only as hashes
- Expire within 24 hours
- Be single use
- Never appear in logs

---

# 10. Core models

All public identifiers MUST use UUIDs.

Every department-owned object must contain or unambiguously derive its department.

## 10.1 User

```text
User
- id
- email
- display_name
- password_hash
- is_active
- mfa_enabled
- created_at
- last_login
```

## 10.2 SystemRole

```text
SystemRole
- id
- user
- role: SYSTEM_ADMIN
- active
- created_at
- created_by
```

## 10.3 Department

```text
Department
- id
- name
- short_code
- status: ACTIVE | SUSPENDED | DEACTIVATED
- created_at
- created_by
```

Suspended or deactivated departments cannot:

- Adopt tablets
- Reactivate tablets
- Extend leases
- Download datasets

## 10.4 DepartmentMembership

```text
DepartmentMembership
- id
- user
- department
- role: DEPARTMENT_ADMIN
- active
- created_at
- created_by
- revoked_at
- revoked_by
```

## 10.5 Station

```text
Station
- id
- department
- name
- short_code
- address, optional
- active
- created_at
- updated_at
```

## 10.6 StationAdminAssignment

```text
StationAdminAssignment
- id
- user
- station
- active
- created_at
- created_by
- revoked_at
- revoked_by
```

## 10.7 Vehicle

```text
Vehicle
- id
- department
- station
- display_name
- call_sign, optional
- asset_identifier, optional
- active
- created_at
- updated_at
```

Vehicle and station must belong to the same department.

## 10.8 Person

```text
Person
- id
- department
- personnel_number, nullable after anonymization
- first_name, nullable after anonymization
- last_name, nullable after anonymization
- display_name
- lifecycle_status:
    ACTIVE
    DEPARTED
    ANONYMIZED
- active
- incident_commander_eligible
- incident_commander_email, nullable
- email_verified_at, nullable
- email_verified_by, nullable
- departed_at, nullable
- retention_until, nullable
- anonymized_at, nullable
- anonymized_by, nullable
- created_at
- updated_at
```

Rules:

- Personnel number is unique within the department while present.
- Commander email is distributed only when commander eligibility is active and the email has been verified.
- Departed personnel must be removed from new tablet publications immediately.
- Do not store medical, attendance, exposure, or incident-history data.
- Identifying data must not be retained indefinitely without a documented legal or operational need.

## 10.9 PersonnelStationAssignment

```text
PersonnelStationAssignment
- id
- person
- station
- assignment_type: HOME | TEMPORARY
- valid_from
- valid_until, nullable
- reason, optional
- created_by
- created_at
- ended_by, nullable
- ended_at, nullable
```

Rules:

- Every active person has exactly one current HOME assignment.
- HOME assignments cannot overlap.
- Temporary assignments may coexist with the home assignment.
- Temporary assignments should normally have an expiry.
- Person and station must belong to the same department.
- History is immutable and must not be overwritten.
- Assignments for departed personnel must be closed.

## 10.10 Tablet

```text
Tablet
- id
- department
- asset_number
- display_name
- status:
    PENDING
    ACTIVE
    STALE
    REMOVED
    LOST
    RETIRED
- created_at
- created_by
- removed_at, nullable
- removed_by, nullable
```

## 10.11 TabletVehicleAssignment

```text
TabletVehicleAssignment
- id
- tablet
- vehicle
- valid_from
- valid_until, nullable
- created_by
- created_at
- ended_by, nullable
- ended_at, nullable
- reason, optional
```

Rules:

- A tablet has at most one current vehicle assignment.
- Tablet and vehicle belong to the same department.
- The tablet’s station is derived from the assigned vehicle.
- Assignment history is retained.

## 10.12 AppInstallation

```text
AppInstallation
- id
- tablet
- installation_uuid
- credential_hash
- status:
    ACTIVE
    STALE
    REVOKED
    REPLACED
- app_version
- hpke_public_key
- hpke_ciphersuite
- hpke_key_fingerprint
- hpke_key_verified_at
- adopted_at
- adopted_by
- last_successful_check_in_at
- authorization_valid_until
- stale_at, nullable
- reactivated_at, nullable
- reactivated_by, nullable
- revoked_at, nullable
- revocation_reason, nullable
```

Rules:

- Only one active or stale installation is permitted per tablet.
- Reinstallation creates a new installation.
- Activating a replacement marks the old installation `REPLACED`.
- The HPKE private key must never leave the tablet.
- The submitted public key must be verified through proof of private-key possession.
- Plaintext installation credentials must never be stored or logged.
- The server supports exactly one configured HPKE cipher suite for the MVP.
- Client requests specifying any other cipher suite must be rejected.

## 10.13 AdoptionInvitation

```text
AdoptionInvitation
- id
- tablet
- token_hash
- expires_at
- created_at
- created_by
- used_at, nullable
- revoked_at, nullable
- failed_attempt_count
```

Only department administrators may create adoption invitations.

## 10.14 AdoptionRequest

```text
AdoptionRequest
- id
- invitation
- installation_uuid
- app_version
- hpke_public_key
- hpke_public_key_fingerprint
- hpke_ciphersuite
- challenge_nonce_hash
- canonical_context_hash
- encrypted_challenge
- expires_at
- completed_at, nullable
- failed_attempt_count
```

Requirements:

- Challenge lifetime: maximum five minutes.
- Challenge is single use.
- Challenge nonce: 256 cryptographically secure random bits.
- Challenge must be bound to:
  - Protocol label
  - Adoption request UUID
  - Installation UUID
  - Tablet UUID
  - Public-key fingerprint
  - HPKE cipher suite
  - Expiration timestamp
- Completion must use constant-time comparison.
- Expired or completed requests cannot be replayed.

## 10.15 ReactivationInvitation

```text
ReactivationInvitation
- id
- app_installation
- token_hash
- expires_at
- created_at
- created_by
- used_at, nullable
- revoked_at, nullable
- failed_attempt_count
```

Only department administrators may create reactivation invitations.

## 10.16 Hydrant

```text
Hydrant
- id
- department
- external_identifier, optional
- location: PostGIS Point, EPSG:4326
- hydrant_type, optional
- flow_information, optional
- status, optional
- source_metadata: JSON
- active
- created_at
- updated_at
```

## 10.17 FirePlan

```text
FirePlan
- id
- department
- object_name
- object_reference, optional
- address, optional
- location: PostGIS Point, nullable
- document_path
- original_filename
- file_size
- page_count
- sha256
- active
- created_at
- updated_at
- uploaded_by
```

Requirements:

- PDF only
- Reject encrypted or password-protected PDFs
- Reject malformed PDFs
- Reject embedded attachments
- Reject active content that cannot be removed
- Validate MIME type and file signature
- Process uploads through quarantine and sanitization
- Use generated storage filenames
- Store outside public static directories
- Apply file-size, page-count, processing-time, and output-size limits

## 10.18 DatasetScopeState

```text
DatasetScopeState
- id
- department
- station, nullable
- dataset_type:
    DEPARTMENT_HYDRANTS
    DEPARTMENT_FIRE_PLANS
    STATION_PERSONNEL
- source_revision
- dirty_since, nullable
- latest_built_publication, nullable
- current_published_publication, nullable
- updated_at
```

Constraints:

- Hydrants and fire plans have no station.
- Personnel scope requires a station.
- One state row exists per type and scope.
- `source_revision` increases whenever tablet-visible canonical data changes.

## 10.19 DatasetPublication

```text
DatasetPublication
- id
- department
- station, nullable
- dataset_type
- version_number
- schema_version
- source_revision
- status:
    BUILDING
    READY_FOR_REVIEW
    PUBLISHED
    FAILED
    SUPERSEDED
    REJECTED
- encrypted_artifact_filename
- encrypted_artifact_size
- ciphertext_sha256
- content_encryption_algorithm
- content_encryption_nonce
- encrypted_content_key
- server_key_version
- publication_metadata_signature
- created_at
- created_by
- published_at, nullable
- published_by, nullable
- supersedes, nullable
- build_error, nullable
```

Rules:

- Every publication receives a new random content-encryption key.
- Published artifacts are immutable.
- Only encrypted artifacts are downloadable.
- Content keys are stored only after encryption under a server key-encryption key.
- Only one publication per dataset type and scope is current.
- New drafts do not become tablet-visible until published.
- The stored artifact filename must be generated from the publication UUID.
- No user-supplied filename or path may be used for protected downloads.

## 10.20 DatasetKeyGrant

```text
DatasetKeyGrant
- id
- publication
- app_installation
- hpke_ciphersuite
- hpke_encapsulated_key
- hpke_wrapped_content_key
- created_at
- revoked_at, nullable
```

Constraint:

```text
Unique publication + app installation
```

Rules:

- The full dataset artifact is not duplicated.
- Only the small content key is wrapped separately for each installation.
- Grants may be generated lazily when the manifest is requested.
- A grant may be created only when the installation is authorized for the publication.
- Grants for revoked or replaced installations must be marked revoked.
- A stale installation cannot receive new grants.

## 10.21 PublicationJob

```text
PublicationJob
- id
- dataset_type
- department
- station, nullable
- source_revision
- status:
    PENDING
    RUNNING
    SUCCEEDED
    FAILED
    OBSOLETE
- requested_by, nullable
- trigger_type:
    USER_REQUEST
    DATA_CHANGE
- created_at
- started_at
- heartbeat_at, nullable
- completed_at
- error_message
```

Requirements:

- At most one `PENDING` or `RUNNING` job may exist for one dataset scope.
- Workers must claim jobs using transactional row locking.
- Use `select_for_update(skip_locked=True)`.
- A worker must record the source revision captured when the job starts.
- Before finalizing a draft, the worker must compare the job revision with the current scope revision.
- If the source revision changed during the build:
  - The completed artifact must not replace the latest draft.
  - The job becomes `OBSOLETE`.
  - A new job must be queued.
- Job processing must be idempotent.
- A crashed `RUNNING` job must be recoverable using heartbeat and timeout rules.

Use PostgreSQL as the job store.

No Redis or Celery is required.

## 10.22 AuditEvent

```text
AuditEvent
- id
- timestamp
- actor_user, nullable
- actor_installation, nullable
- action
- department, nullable
- station, nullable
- target_type
- target_uuid, nullable
- request_id
- source_ip
- user_agent
- metadata: JSON
```

Audit events are append-only.

Database requirements:

- The runtime database role may `INSERT` and `SELECT`.
- The runtime role must not have `UPDATE`, `DELETE`, or `TRUNCATE`.
- The runtime role must not own the table.
- A database trigger must reject `UPDATE`, `DELETE`, and `TRUNCATE`.
- Only the migration/database-owner role may modify the schema.

Audit metadata must not include complete personnel records or secrets.

---

# 11. Automatic dataset draft generation

Canonical data changes must automatically identify and rebuild affected dataset scopes.

Automatic generation creates a `READY_FOR_REVIEW` draft. It does not publish the draft.

Only a department administrator can publish a new version.

## 11.1 General process

```text
Authorized administrator changes canonical data
→ database transaction succeeds
→ affected dataset scopes are marked dirty
→ source revisions are incremented
→ publication jobs are queued after transaction commit
→ workers claim jobs with row-level locks
→ encrypted draft packages are built
→ draft revision is checked against current source revision
→ valid drafts become READY_FOR_REVIEW
→ department administrator reviews and publishes
→ tablets see the new version in their manifests
```

Use `transaction.on_commit` or an equivalent pattern so jobs are not queued for rolled-back changes.

Do not create duplicate pending jobs for the same dataset type and scope.

## 11.2 Hydrant changes

Creating, editing, activating, deactivating, or importing hydrants:

```text
Marks DEPARTMENT_HYDRANTS dirty
→ increments source revision
→ queues one department hydrant draft
```

## 11.3 Fire-plan changes

Adding, replacing, editing, activating, or deactivating a fire plan:

```text
Marks DEPARTMENT_FIRE_PLANS dirty
→ increments source revision
→ queues one department fire-plan draft
```

For the beta, the resulting publication contains one monolithic encrypted archive of all current department fire plans.

## 11.4 Personnel changes

Creating, editing, activating, deactivating, offboarding, or anonymizing a person marks every station package in which that person is currently visible as dirty.

Changing commander eligibility or verified commander email marks every station package in which the person is visible as dirty.

## 11.5 Permanent home-station transfer

When a department administrator transfers a person from Station A to Station B:

```text
Close Station A HOME assignment
→ create Station B HOME assignment
→ mark Station A personnel scope dirty
→ mark Station B personnel scope dirty
→ increment both revisions
→ queue draft packages for both stations
```

The Station A draft removes the person.

The Station B draft adds the person.

Neither draft becomes available to tablets until published.

## 11.6 Temporary assignment

When a department administrator temporarily assigns a Station A person to Station B:

```text
Create Station B TEMPORARY assignment
→ mark Station B personnel scope dirty
→ queue Station B draft
```

Station A remains the home station and normally requires no rebuild.

When the temporary assignment expires or ends:

```text
Mark Station B personnel scope dirty
→ queue Station B draft
```

A periodic management command must detect expired temporary assignments.

## 11.7 Publication control

Department administrators can:

- Review a draft
- Inspect its source changes
- Publish it
- Reject it
- Trigger a rebuild
- Roll back to an earlier published version

Existing tablets receive only current `PUBLISHED` versions.

---

# 12. Dataset encryption architecture

## 12.1 Goals

The encryption design must ensure:

- Each dataset publication is packaged and encrypted once.
- The server does not create a full encrypted copy per tablet.
- Only authorized adopted app installations can obtain the package key.
- Station-specific personnel keys are granted only to tablets assigned to that station.
- Copying an encrypted artifact from server storage is insufficient to read it.
- Copying another tablet’s key grant is insufficient to decrypt it.

## 12.2 Publication encryption

For every new dataset publication:

1. Generate a new random 256-bit content-encryption key, or CEK.
2. Generate a unique nonce.
3. Encrypt the complete package using AES-256-GCM.
4. Calculate SHA-256 over the ciphertext.
5. Encrypt the CEK using a server key-encryption key.
6. Store:
   - One encrypted artifact
   - The encrypted CEK
   - Encryption metadata
   - Ciphertext hash
7. Sign the publication metadata using Ed25519.

A CEK and nonce pair must never be reused.

A new publication version must receive a new CEK.

## 12.3 Server key-encryption key

The server key-encryption key, or KEK:

- Must be stored outside the repository.
- Must never be stored in plaintext in PostgreSQL.
- Must have a version identifier.
- Must be backed up securely.
- Must support future rotation.
- Must never be returned through an API.
- Must be accessible only to the process that creates publications and key grants.
- Must not require running Django or Gunicorn as root.

Preferred delivery:

```text
systemd LoadCredential=
```

The service reads the credential through the systemd-managed credential path.

Permitted beta fallback:

```text
Owner: root
Group: fire_backend_crypto
Mode: 0440
```

The publication/key-grant process runs under an unprivileged user belonging to that group.

A root-owned `0600` file that the application cannot read is not a valid configuration.

Where practical, separate:

- Web/API service account
- Publication/key-grant service account

The web service should not receive broader KEK access than required.

## 12.4 Tablet HPKE key

During adoption, the app creates an HPKE-compatible public/private key pair.

The tablet:

- Keeps the private key locally.
- Sends only the public key.
- Proves possession of the private key.
- Must store the private key using iOS Keychain or Secure Enclave-backed facilities where supported.

The backend stores:

- Public key
- Cipher suite
- Fingerprint
- Verification timestamp

## 12.5 HPKE cipher suite

For the MVP:

- Exactly one HPKE cipher suite is supported.
- The suite is configured server-side.
- The iOS client uses the same fixed suite.
- The client may report the suite identifier, but it may not negotiate an arbitrary suite.
- Any request using a different suite must be rejected.
- The selected suite must pass Python-to-Swift interoperability tests before beta deployment.

The exact suite should be selected based on tested compatibility with the intended iOS key-storage implementation.

## 12.6 HPKE key grant

When an authorized installation requests its manifest:

1. Resolve the installation’s department and station.
2. Confirm that the installation is active and its lease is valid.
3. Select current authorized publications.
4. For each publication:
   - Retrieve the encrypted CEK.
   - Decrypt the CEK using the server KEK.
   - Use RFC 9180 HPKE to encrypt the CEK to the installation’s public key.
   - Create or retrieve a `DatasetKeyGrant`.
5. Return the grant in the signed manifest.
6. Clear plaintext CEK material from application memory as soon as practical.

The encrypted dataset artifact remains identical for every authorized tablet.

## 12.7 HPKE associated information

The HPKE operation must bind the wrapped CEK to canonical associated information containing at least:

- Protocol label
- Publication UUID
- Installation UUID
- Tablet UUID
- Dataset type
- Department UUID
- Station UUID where applicable
- Publication version
- Schema version
- Ciphertext SHA-256

A grant copied to another installation, tablet, publication, or scope must fail to open.

## 12.8 Signed manifest

The manifest must be signed using Ed25519.

The signature must cover canonical serialized data including:

- Installation UUID
- Tablet UUID
- Department UUID
- Station UUID
- Vehicle UUID
- Manifest generation
- Authorization expiry
- Publication identifiers
- Dataset types and scopes
- Versions
- Ciphertext hashes
- Encryption algorithms
- HPKE cipher suite
- HPKE encapsulated keys
- Wrapped CEKs

The tablet application will embed the corresponding public verification key.

## 12.9 Division of protections

```text
HTTPS
→ protects the active connection

AES-256-GCM encrypted artifact
→ protects the stored and downloaded package

HPKE key grant
→ restricts the package key to one adopted app installation

Ed25519 signature
→ proves manifest and publication authenticity

Seven-day authorization lease
→ limits offline use
```

HPKE does not remotely erase already decrypted data. The tablet application must enforce lease expiry and delete decrypted keys and plaintext caches.

---

# 13. HPKE adoption challenge

## 13.1 Challenge construction

The server creates a canonical adoption context containing:

- Protocol label: `tablet-adoption-v1`
- Adoption request UUID
- Installation UUID
- Tablet UUID
- HPKE public-key fingerprint
- Configured HPKE cipher suite
- Challenge expiration timestamp
- Random 256-bit nonce

The server HPKE-encrypts the nonce to the submitted public key.

The protocol label and canonical adoption context must be supplied through HPKE context information and authenticated associated data where supported.

## 13.2 Challenge response

The tablet decrypts the nonce and returns:

```text
HMAC-SHA256(
    key = decrypted_nonce,
    message = canonical_adoption_context
)
```

The backend must:

- Reconstruct the canonical context server-side.
- Verify the HMAC using constant-time comparison.
- Accept the response only for the original adoption request.
- Reject mismatched installation UUIDs.
- Reject mismatched public-key fingerprints.
- Reject mismatched cipher suites.
- Reject expired, completed, or revoked requests.
- Limit failed attempts.
- Mark the challenge used atomically with installation creation.

The plaintext nonce must never be returned by any endpoint.

---

# 14. Tablet authorization lease

## 14.1 Default lease

Each active installation receives a configurable authorization lease.

Default:

```text
7 days
```

Tablets should check in whenever connectivity is available, preferably at least daily.

## 14.2 Successful check-in

A successful check-in verifies:

- Installation credential
- Installation status
- Tablet status
- Department status
- Station status
- Vehicle status
- Supported app version
- Authorization scope

If the existing lease has not expired:

```text
authorization_valid_until = server_time + 7 days
```

The backend returns:

```json
{
  "status": "active",
  "server_time": "2026-08-05T08:00:00Z",
  "authorization_valid_until": "2026-08-12T08:00:00Z",
  "minimum_app_version": "1.0.0",
  "manifest_generation": 42,
  "lease_signature": "base64"
}
```

The lease signature covers at least:

- Installation UUID
- Tablet UUID
- Authorization expiry
- Department UUID
- Station UUID
- Issued-at timestamp

## 14.3 Stale transition

If the installation does not check in before expiry:

- Installation status becomes `STALE`.
- Tablet status becomes `STALE` where appropriate.
- Ordinary check-in cannot reactivate it.
- Manifest and dataset endpoints are denied.
- New HPKE key grants are denied.
- Only status and reactivation endpoints remain available.

Stale state may be applied:

- By a scheduled management command
- Lazily during a request
- Both

## 14.4 Tablet-side contract

The iOS implementation is outside this PRD, but the API contract requires the client to:

- Store and verify the signed lease.
- Stop using provisioned data after expiry.
- Delete unwrapped CEKs.
- Delete plaintext personnel and fire-plan caches.
- Retain encrypted artifacts only when they cannot be opened.
- Require department-admin reactivation.
- Resist simple device-clock rollback by anchoring expiry to verified server time and monotonic elapsed time.

The backend cannot purge a completely offline device by itself.

---

# 15. Tablet adoption

Only department administrators may perform initial adoption.

The administrator must have physical access to the tablet.

## 15.1 Administrator workflow

1. Create or select a tablet.
2. Assign it to an active vehicle.
3. Verify the derived station and department.
4. Generate a one-time adoption QR code.
5. Open the app on the tablet.
6. Scan the QR code.
7. Verify department, station, vehicle, and asset number.
8. Complete HPKE proof of private-key possession.
9. Confirm adoption.

## 15.2 Adoption preview

```http
POST /api/v1/adoption/preview
```

Request:

```json
{
  "token": "one-time-token",
  "installation_uuid": "uuid",
  "app_version": "1.0.0",
  "hpke_public_key": "base64",
  "hpke_ciphersuite": "configured-suite"
}
```

The backend:

- Validates the invitation.
- Validates the assignment.
- Confirms that the requested cipher suite is the configured suite.
- Creates an adoption request.
- Generates the bound challenge.
- Encrypts the nonce to the submitted public key using HPKE.

## 15.3 Adoption completion

```http
POST /api/v1/adoption/complete
```

Request:

```json
{
  "adoption_request_id": "uuid",
  "challenge_response": "base64",
  "confirmed": true
}
```

The backend then:

1. Verifies proof of private-key possession.
2. Creates the app installation.
3. Replaces any previous installation.
4. Generates a random 256-bit installation credential.
5. Stores only the credential hash.
6. Returns the credential once.
7. Creates a seven-day lease.
8. Marks the invitation and challenge used atomically.
9. Marks the tablet active.
10. Records an audit event.

---

# 16. Stale-tablet reactivation

Only department administrators may reactivate stale tablets.

Physical access is required for the MVP.

## 16.1 Workflow

1. Department administrator opens the stale tablet record.
2. Administrator confirms it is not lost, removed, or retired.
3. Administrator generates a short-lived reactivation QR code.
4. The stale app scans the code.
5. The backend verifies:
   - Reactivation invitation
   - Existing installation identity
   - HPKE private-key possession
   - Department, station, vehicle, and tablet status
6. The backend rotates the installation credential.
7. The backend creates a new seven-day lease.
8. Key grants may be generated only after authorization is restored.
9. The tablet downloads the current manifest.
10. An audit event is created.

A tablet marked `LOST`, `REMOVED`, or `RETIRED` cannot be reactivated through this workflow.

---

# 17. Tablet removal

A department administrator may mark a tablet:

- Removed
- Lost
- Retired

Removal must:

1. Revoke active or stale installations.
2. Revoke outstanding invitations.
3. Revoke server-side dataset key grants.
4. Reject future check-ins.
5. Reject future manifests and downloads.
6. Record reason, actor, and time.
7. Create an audit event.

A revoked credential may call only the status endpoint to receive:

```json
{
  "status": "revoked",
  "purge_provisioned_data": true
}
```

Removing the corresponding private-network node is outside this PRD.

---

# 18. Fire-plan upload security

Fire-plan PDFs are untrusted input.

## 18.1 Processing flow

```text
Upload received
→ request-size check
→ extension and MIME validation
→ write to quarantine
→ run isolated sanitizer subprocess
→ enforce resource limits
→ strip prohibited content
→ reopen and validate sanitized output
→ calculate hash
→ move sanitized file to accepted storage
→ delete quarantine input
```

No unsanitized PDF may be moved into final fire-plan storage.

## 18.2 Sanitization requirements

The sanitizer must remove or reject:

- `/JS`
- `/JavaScript`
- `/Launch`
- `/SubmitForm`
- `/ImportData`
- Embedded files and attachments
- PDF portfolios
- Rich media
- Multimedia
- External file references
- External streams
- Actions that initiate external communication
- Forms or actions that cannot be safely flattened or removed
- Unparseable object trees
- Malformed cross-reference structures
- Unsupported non-standard streams

After sanitization, the output must be reopened and validated.

If sanitization or reopening fails, reject the upload.

## 18.3 Sandbox and resource limits

PDF processing must occur:

- In a separate subprocess
- Under a dedicated unprivileged operating-system user
- Without network access
- With read-only access to the quarantine input
- With write access only to a temporary output directory
- With a wall-clock timeout
- With a CPU-time limit
- With a memory limit
- With a maximum output-file size
- With a maximum page count
- With limits on decompressed stream sizes where supported

The sanitizer process must not have access to:

- Django secrets
- Database credentials
- Server KEKs
- Signing keys
- Backup credentials
- Accepted document storage beyond its output target

---

# 19. Provisioning and update API

Use:

> One check-in endpoint, one manifest endpoint, and one generic download endpoint for individual publications.

Do not create separate update-check APIs for hydrants, fire plans, and personnel.

Do not combine hydrants, fire plans, and personnel into one archive.

## 19.1 API base

```text
/api/v1/
```

## 19.2 Tablet endpoints

```http
POST /api/v1/tablet/check-in
GET  /api/v1/tablet/status
GET  /api/v1/tablet/configuration
GET  /api/v1/tablet/manifest
GET  /api/v1/tablet/datasets/{publication_uuid}/download
POST /api/v1/tablet/reactivation/complete
```

Authentication:

```http
Authorization: Bearer <installation-credential>
```

Only the credential hash is stored.

## 19.3 Manifest authorization

The backend derives scope through:

```text
AppInstallation
→ Tablet
→ current TabletVehicleAssignment
→ Vehicle
→ Station
→ Department
```

The tablet must not submit or choose an arbitrary department or station.

The manifest returns:

- Current published department hydrants
- Current published department fire plans
- Current published personnel package for the derived station
- One HPKE key grant per listed publication
- Current lease expiry
- Configuration metadata

## 19.4 Example manifest

```json
{
  "manifest_generation": 42,
  "generated_at": "2026-08-05T08:00:00Z",
  "authorization_valid_until": "2026-08-12T08:00:00Z",
  "configuration": {
    "department_id": "uuid",
    "station_id": "uuid",
    "vehicle_id": "uuid",
    "tablet_id": "uuid"
  },
  "datasets": [
    {
      "publication_id": "uuid",
      "type": "department_hydrants",
      "scope": "department",
      "version": 8,
      "schema_version": 1,
      "encrypted_size": 123456,
      "ciphertext_sha256": "hex",
      "content_encryption_algorithm": "AES-256-GCM",
      "download_url": "/api/v1/tablet/datasets/uuid/download",
      "key_grant": {
        "scheme": "HPKE",
        "ciphersuite": "configured-suite",
        "encapsulated_key": "base64",
        "wrapped_content_key": "base64"
      }
    }
  ],
  "signature": "base64"
}
```

## 19.5 Tablet update behavior

The tablet compares publication UUIDs or versions with locally installed versions.

Each package is downloaded independently.

For the beta:

| Dataset | Update mechanism |
|---|---|
| Hydrants | Complete encrypted snapshot |
| Station personnel | Complete encrypted station snapshot |
| Department fire plans | Complete encrypted department archive |

A new fire-plan publication requires downloading the complete fire-plan archive.

Incremental per-document fire-plan updates are deferred until after beta.

## 19.6 Protected dataset download

Django must authorize the publication and return an internal redirect.

Required Nginx pattern:

```nginx
location /internal-protected-datasets/ {
    internal;
    alias /var/lib/fire-backend/encrypted-artifacts/;
}
```

Django may return only a server-generated filename:

```text
<publication_uuid>.bin
```

Example internal redirect:

```text
/internal-protected-datasets/<publication_uuid>.bin
```

Django must not use:

- Original uploaded filenames
- Client-provided paths
- Database path values derived from requests
- Relative path segments
- User-controlled subdirectories
- `..`
- Path separators supplied by the client

The publication UUID must be parsed and validated before authorization.

Physical paths must be derived entirely from trusted server configuration and the validated publication UUID.

The protected storage directory must not contain secrets or unrelated application files.

## 19.7 Download authorization

Before sending `X-Accel-Redirect`, Django verifies:

- Installation is active.
- Lease is unexpired.
- Tablet is active.
- Department is active.
- Publication is published.
- Publication belongs to the tablet’s department.
- Station matches for station-specific personnel.
- Publication is present in the installation’s current authorized manifest.

## 19.8 Caching and resumption

Support:

- `ETag`
- `If-None-Match`
- `304 Not Modified`
- HTTP range requests for large fire-plan downloads
- Safe retry of interrupted downloads
- Stable ciphertext hashes

## 19.9 Errors

Use `application/problem+json`.

Example:

```json
{
  "type": "https://backend.example/problems/tablet-authorization-stale",
  "title": "Tablet authorization expired",
  "status": 403,
  "detail": "A department administrator must reactivate this tablet.",
  "request_id": "uuid"
}
```

## 19.10 API documentation

Generate and validate OpenAPI 3.1 using `drf-spectacular`.

Do not include real personnel, tokens, email addresses, or filenames in examples.

---

# 20. Dataset formats

## 20.1 Hydrants

Plain package before encryption:

```text
UTF-8 GeoJSON
Optional gzip compression
```

Validation includes:

- Valid GeoJSON
- Point geometry only
- Valid longitude and latitude
- Maximum feature count
- Duplicate reporting
- Transactional import
- Preview before publication

## 20.2 Personnel

Plain package before encryption:

```text
UTF-8 JSON
```

Include only:

- Stable person UUID
- Display name
- Personnel number only when required by the app
- Incident-commander eligibility
- Verified incident-commander email where applicable

A station package contains:

- Active home personnel for that station
- Active temporary personnel assigned to that station

It must exclude:

- Departed personnel
- Anonymized personnel
- Personnel belonging only to another station
- Unverified commander email addresses

## 20.3 Fire plans

Plain package before encryption:

```text
ZIP archive
├── manifest.json
└── sanitized PDF documents
```

The ZIP is encrypted as one publication artifact.

---

# 21. Personnel retention and offboarding

## 21.1 Purpose

Personnel data must not be retained indefinitely without a documented purpose.

Retention periods must be configurable by department policy and documented with the relevant data-protection officer.

The backend must support a lifecycle rather than permanent soft deletion.

## 21.2 Offboarding workflow

When a person leaves the department:

1. Set lifecycle status to `DEPARTED`.
2. Set `departed_at`.
3. End active home and temporary assignments.
4. Disable commander eligibility.
5. Clear or invalidate the commander email.
6. Remove the person from all newly generated tablet packages.
7. Mark affected station datasets dirty.
8. Set `retention_until` based on department policy.
9. Create an audit event.

## 21.3 Anonymization

After the approved retention period, an authorized department administrator may anonymize the person.

Anonymization must:

- Remove first name
- Remove last name
- Remove email
- Remove personnel number or replace it with an irreversible non-identifying value
- Replace display name with a neutral value such as `Former member`
- Preserve the stable UUID where needed for audit references
- Preserve non-identifying assignment timestamps only where required
- Record anonymization date and administrator
- Create an audit event

Audit events should refer to the stable person UUID, not retain names or emails unnecessarily.

## 21.4 Hard deletion

Hard deletion may be allowed only when:

- No legal or operational retention obligation applies.
- Referential integrity can be preserved.
- The department’s approved policy permits it.
- An authorized department administrator confirms the action.

Where historical assignment records must remain, anonymization is preferred over hard deletion.

---

# 22. Permission implementation

## 22.1 Default deny

Every endpoint and view must explicitly define:

- Authentication
- Permission class
- Department scope
- Station scope
- Allowed methods

## 22.2 Scoped queries

Forbidden:

```python
Person.objects.all()
```

Required pattern:

```python
personnel_service.visible_to_user(
    user=request.user,
    department_id=department_id,
)
```

Company system administrators receive no operational query access.

## 22.3 Object authorization

Authorization must be applied before retrieving or serializing an object addressed by UUID.

Knowing another object’s UUID must not grant access.

## 22.4 Writable fields

Forms and serializers must explicitly list writable fields.

Client input must not directly control:

- Department ownership
- Station ownership
- Assignment activation
- Publication status
- Created-by fields
- Verified-by fields
- Authorization state
- Encryption metadata
- Key grants
- Audit metadata
- Retention bypass fields

## 22.5 Database roles

Use separate PostgreSQL roles:

```text
database_owner
application_runtime
backup_role
```

The Django runtime role must not:

- Be a superuser
- Own the schema
- Have `BYPASSRLS`
- Update or delete audit events

PostgreSQL Row-Level Security may be used as defense in depth.

---

# 23. Database-level audit protection

Required database privileges:

```sql
REVOKE ALL ON TABLE audit_event FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_event
FROM application_runtime;
GRANT SELECT, INSERT ON TABLE audit_event
TO application_runtime;
```

Add a database trigger that raises an exception for:

- `UPDATE`
- `DELETE`
- `TRUNCATE`

The trigger must be installed through a Django migration executed by the database-owner role.

Tests must verify that the runtime role cannot mutate or delete audit rows.

---

# 24. Security requirements

Target OWASP ASVS Level 2.

## 24.1 Transport

- HTTPS only
- Gunicorn bound to localhost or a Unix socket
- PostgreSQL bound locally only
- No Django development server
- Explicit `ALLOWED_HOSTS`
- Correct proxy-header configuration

## 24.2 Django settings

```text
DEBUG = False
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = Lax
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = DENY
```

## 24.3 Rate limits

Rate-limit:

- Administrator login
- MFA verification
- Account setup
- Adoption preview and completion
- Reactivation
- Invalid tablet authentication
- Invalid download attempts
- File-upload validation failures

## 24.4 Input validation

- Reject unknown fields.
- Validate lengths.
- Normalize email addresses.
- Use enum allowlists.
- Limit body and upload sizes.
- Limit GeoJSON features.
- Validate file signatures.
- Never trust client-provided ownership fields.
- Never trust client-provided cipher-suite choices.
- Never trust client-provided artifact paths.

## 24.5 Secrets and cryptographic keys

Store outside the repository:

- Django secret key
- Database credentials
- Dataset signing private key
- Server key-encryption keys
- Backup credentials

Use separate keys for:

- Ed25519 signing
- Server-side CEK encryption

Do not reuse one key for both purposes.

Prefer systemd credentials for runtime secret delivery.

## 24.6 Logging

Never log:

- Passwords
- MFA secrets
- Adoption or reactivation tokens
- Installation credentials
- Authorization headers
- Decrypted CEKs
- Server KEKs
- HPKE private keys
- HPKE challenge nonces
- Full request bodies
- Personnel exports
- Fire-plan contents

## 24.7 Dependency security

CI must run:

- `pip-audit`
- `bandit`

Dependencies must be pinned and security updates applied promptly.

---

# 25. Audit requirements

Audit:

- Login success and failure
- MFA enrollment and reset
- Account creation and disabling
- Role grants and revocations
- Department status changes
- Station and vehicle changes
- Personnel creation and editing
- Personnel departure
- Personnel anonymization or deletion
- Home-station transfers
- Temporary assignments
- Commander eligibility changes
- Email verification changes
- Dataset scopes marked dirty
- Automatic publication jobs
- Publication build success, failure, or obsolescence
- Publication approval
- Rollback
- Tablet creation
- Adoption-code generation
- Adoption success or failure
- HPKE-key verification
- Invalid cipher-suite attempts
- Check-in failures
- Tablet becoming stale
- Reactivation-code generation
- Reactivation success or failure
- Credential rotation
- Tablet removal
- Unauthorized access attempts
- PDF rejection and sanitization failure

Audit events must be append-only.

---

# 26. Backups and operations

Implement:

- Nightly PostgreSQL `pg_dump`
- Nightly backup of sanitized fire plans
- Nightly backup of encrypted publication artifacts
- Secure backup of signing and server encryption keys
- Encrypted `restic` backups
- At least one off-Proxmox destination
- Retention policy
- Backup-failure logging
- Monthly restore tests during beta

Provide:

```http
GET /health/live
GET /health/ready
```

Use:

- Gunicorn systemd service
- Publication-job systemd timer
- Temporary-assignment-expiry timer
- Stale-installation timer
- Personnel-retention timer or report
- Backup timer
- Log rotation
- Automatic restart
- Time synchronization

---

# 27. Testing requirements

## 27.1 Authorization tests

1. System admin can create a department.
2. System admin can create the first department admin.
3. System admin cannot list or retrieve personnel.
4. System admin cannot access hydrants, fire plans, or datasets.
5. Department admin can create another department admin.
6. Department admin cannot access another department.
7. Department admin may also hold station-admin assignments.
8. Station admin can access assigned-station personnel.
9. Station admin cannot access another station’s personnel.
10. Station admin cannot perform tablet adoption.
11. Station admin cannot reactivate stale tablets.
12. Station admin cannot change personnel home stations.
13. Station admin cannot create cross-station temporary assignments.
14. Tablet receives department-wide publications.
15. Tablet receives only its station personnel publication.
16. Tablet cannot request another station’s package by UUID.
17. Revoked or stale installations cannot receive key grants.
18. Revoked or stale installations cannot download datasets.
19. Expired and reused invitation tokens fail.
20. Active check-in extends an unexpired lease.
21. An expired lease becomes stale.
22. A stale tablet cannot self-reactivate.
23. Department-admin reactivation rotates the credential.

## 27.2 Automatic update and job tests

24. Creating Station A personnel queues a Station A draft.
25. Editing Station A personnel queues a Station A draft.
26. Changing commander status queues each affected station draft.
27. Moving a person from Station A to Station B queues both station drafts.
28. A temporary Station B assignment queues Station B only.
29. Ending or expiring the temporary assignment queues Station B.
30. Hydrant changes queue the department hydrant draft.
31. Fire-plan changes queue the department fire-plan draft.
32. Automatically generated drafts are not tablet-visible until published.
33. Duplicate changes do not create uncontrolled duplicate jobs.
34. Two workers cannot claim the same job.
35. `skip_locked` permits independent jobs to run concurrently.
36. A build whose source revision becomes outdated is marked obsolete.
37. An obsolete build cannot replace the latest draft.
38. A crashed running job can be recovered safely.

## 27.3 Cryptographic tests

39. Every publication receives a new CEK.
40. CEK and nonce pairs are never reused.
41. Stored dataset artifacts are ciphertext.
42. Wrong tablet private key cannot open a key grant.
43. A grant copied to another installation cannot be opened.
44. A grant copied to another publication cannot be opened.
45. Modified HPKE encapsulated key fails.
46. Modified wrapped CEK fails.
47. Modified associated information fails.
48. Modified encrypted package fails SHA-256 verification.
49. Modified manifest fails Ed25519 verification.
50. A non-whitelisted cipher suite is rejected.
51. The HPKE challenge is bound to the adoption request.
52. The HPKE challenge is bound to the installation UUID.
53. The HPKE challenge is bound to the public-key fingerprint.
54. An expired challenge fails.
55. A completed challenge cannot be replayed.
56. Python backend HPKE output can be opened by the Swift test client.

## 27.4 File-security tests

57. Password-protected PDFs are rejected.
58. PDFs containing JavaScript are stripped or rejected.
59. PDFs containing launch actions are stripped or rejected.
60. PDFs containing attachments are stripped or rejected.
61. Malformed PDFs are rejected.
62. PDF processing timeout is enforced.
63. PDF memory limit is enforced.
64. PDF output-size limit is enforced.
65. Sanitized output is reopened before acceptance.
66. Unsanitized files never enter final storage.

## 27.5 Protected-download tests

67. Direct requests to the Nginx internal location return `404`.
68. Authorized Django requests produce a valid internal redirect.
69. Client-supplied paths cannot influence the redirect.
70. `../` and path separators are rejected.
71. Only `<publication_uuid>.bin` artifacts can be served.
72. A known publication UUID from another department is denied.
73. A station tablet cannot download another station’s personnel package.

## 27.6 Audit tests

74. Runtime role can insert audit events.
75. Runtime role can select authorized audit events.
76. Runtime role cannot update audit events.
77. Runtime role cannot delete audit events.
78. Runtime role cannot truncate the audit table.
79. Database trigger rejects mutation attempts.

## 27.7 Retention tests

80. Departed personnel disappear from new packages.
81. Departed personnel assignments are closed.
82. Departed commanders lose commander eligibility.
83. Anonymization removes identifying fields.
84. Audit records retain only stable identifiers where required.
85. Unauthorized users cannot bypass retention controls.

## 27.8 End-to-end test

```text
Create system administrator
→ create department
→ create department administrator
→ create Station A and Station B
→ create vehicles
→ create personnel in Station A
→ automatically build Station A draft
→ publish Station A personnel
→ import and publish hydrants
→ upload, sanitize, and publish fire plans
→ create tablet assigned to Station A vehicle
→ create adoption invitation
→ register and verify HPKE key
→ adopt installation
→ check in
→ request manifest
→ receive shared department packages and Station A package
→ decrypt package CEKs
→ move person from Station A to Station B
→ automatically build both station drafts
→ verify tablets still see old published versions
→ publish both drafts
→ verify Station A and Station B manifests change appropriately
→ simulate lease expiry
→ verify stale access denial
→ reactivate using department-admin invitation
→ verify credential rotation and restored access
→ offboard a person
→ verify removal from the next station package
→ remove tablet
→ verify permanent denial
```

## 27.9 CI checks

```text
ruff
mypy
bandit
pip-audit
pytest
Django system checks
migration consistency
OpenAPI schema generation
cryptographic interoperability tests
database-role permission tests
```

---

# 28. Acceptance criteria

The backend MVP is complete when:

- It runs reproducibly in an unprivileged Debian LXC.
- Tailnet creation is not required by the backend project.
- Django and Gunicorn run as unprivileged service users.
- KEKs are delivered using systemd credentials or correctly restricted group-readable files.
- Company system administrators cannot access operational department data.
- Department administrators can create additional department administrators.
- Department administrators may also hold station-admin assignments.
- Only department administrators can adopt or reactivate tablets.
- Department administrators curate and publish tablet updates.
- Canonical data changes automatically generate affected draft packages.
- Automatic drafts are never published without department-admin approval.
- Publication workers use row locks and source revisions.
- Permanent Station A-to-B personnel transfers generate drafts for both stations.
- Temporary assignments generate drafts for the destination station.
- Department hydrants are packaged and encrypted once per version.
- Department fire plans are packaged and encrypted once per version.
- Station personnel are packaged and encrypted once per station version.
- Full encrypted packages are not duplicated per tablet.
- Small HPKE key grants are created per authorized installation.
- Exactly one HPKE cipher suite is supported and tested.
- HPKE adoption challenges are session- and key-bound.
- One manifest endpoint reports all authorized versions.
- Separate download requests retrieve only changed packages.
- Nginx protected downloads cannot expose arbitrary files.
- Tablets receive department-wide data and only their station’s personnel.
- Seven-day authorization leases are enforced.
- Stale tablets cannot self-reactivate.
- Reactivation rotates the installation credential.
- Removed tablets cannot validate or download updates.
- Fire-plan PDFs are quarantined, resource-limited, sanitized, and revalidated.
- Audit immutability is enforced by PostgreSQL permissions and triggers.
- Departed personnel are removed from new publications.
- Personnel anonymization and retention workflows exist.
- All packages are hashed, encrypted, and authenticated.
- Manifests are digitally signed.
- No backend endpoint accepts incident data.
- Backups and restores have been tested.
- All authorization, publication, lease, cryptographic, file-processing, audit, and retention tests pass.

---

# 29. Implementation order

## Phase 1 — Foundation

- Django project
- PostgreSQL/PostGIS
- Custom user model
- Environment configuration
- Unprivileged service users
- Gunicorn
- Nginx
- systemd
- Health checks
- Test framework

## Phase 2 — Authentication and permissions

- System roles
- Department memberships
- Station-admin assignments
- MFA
- Scoped query services
- Permission tests
- Audit framework
- Database-level audit immutability

## Phase 3 — Organization and assignments

- Departments
- Stations
- Vehicles
- Tablet vehicle assignments
- Personnel station assignments
- Administrator setup workflows

## Phase 4 — Personnel and retention

- Personnel
- Commander eligibility
- Verified email workflow
- Offboarding
- Retention dates
- Anonymization
- Retention tests

## Phase 5 — Reference data

- Hydrant import
- Fire-plan quarantine
- PDF sandboxing
- PDF sanitization
- Validation and accepted storage

## Phase 6 — Publication pipeline

- Dataset scope state
- Source revisions
- Dirty-state detection
- PostgreSQL job queue
- `select_for_update(skip_locked=True)`
- Draft review
- Manual publication
- Rollback
- Expired temporary-assignment processing

## Phase 7 — Cryptography

- Systemd credential delivery
- Server signing keys
- Server KEK management
- Per-publication CEKs
- AES-256-GCM artifact encryption
- Fixed HPKE cipher suite
- HPKE key registration
- Bound HPKE proof of possession
- Dataset key grants
- Signed manifests
- Swift/Python interoperability tests

## Phase 8 — Adoption and leases

- Tablets
- App installations
- Adoption invitations
- Adoption API
- Check-in API
- Seven-day leases
- Stale-state processing
- Reactivation
- Credential rotation
- Revocation

## Phase 9 — Provisioning API

- Configuration endpoint
- Single manifest endpoint
- Protected Nginx download endpoint
- Strict UUID-based path construction
- ETags
- Range support
- RFC 9457 errors
- OpenAPI schema
- End-to-end tests

## Phase 10 — Deployment hardening

- Backups
- Restore procedure
- Logging
- Security checks
- Production Django checklist
- Beta administrator documentation