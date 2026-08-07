opencode# PRD Amendment  
## Extensible Provisioned Dataset Architecture

**PRD version:** 3.2  
**Applies to:** Fire Department Tablet Provisioning Backend — Beta MVP  
**Purpose:** Allow future features such as `Waldbrandeinsatzkarten` to reuse the existing publication, encryption, authorization, manifest, and download architecture.

---

# 1. Design decision

Provisioned dataset types MUST be defined through a **code-level dataset registry**.

Dataset types must not be limited by a PostgreSQL enum or a closed Django model-choice migration.

Administrators must not be able to create arbitrary dataset types through the web interface.

Adding a new dataset type requires:

- Backend implementation
- Validation rules
- Packaging logic
- Tablet-side support
- Automated tests
- A coordinated application release where necessary

The generic provisioning infrastructure must remain reusable across dataset types.

---

# 2. Replace fixed dataset-type enums

Where the PRD currently defines:

```text
dataset_type:
    DEPARTMENT_HYDRANTS
    DEPARTMENT_FIRE_PLANS
    STATION_PERSONNEL
```

replace it with:

```text
dataset_type_code
```

Requirements:

- Stored as a validated string.
- Maximum length: 100 characters.
- Must match a type registered in the backend dataset registry.
- Must use lowercase snake-case identifiers.
- Must not use a PostgreSQL enum.
- Must not be writable directly by administrators or tablet clients.
- Existing type codes are:

```text
department_hydrants
department_fire_plans
station_personnel
```

A future feature may add:

```text
department_wildfire_operation_maps
```

---

# 3. Dataset registry

Add a code-level registry interface.

Conceptual definition:

```python
class DatasetTypeDefinition:
    code: str
    display_name: str
    scope: Literal["department", "station"]
    artifact_format: str
    current_schema_version: int
    encryption_required: bool
    minimum_supported_app_version: str | None
    builder_service: str
    validator_service: str
```

The registry MUST define, for every dataset type:

- Stable type code
- Human-readable display name
- Scope:
  - `department`
  - `station`
- Artifact format
- Current schema version
- Whether encryption is mandatory
- Minimum compatible tablet app version
- Canonical builder service
- Canonical validation service
- Tablet-visible metadata rules

The registry must be immutable at runtime.

Dataset definitions must be reviewed and deployed as application code.

---

# 4. Dataset scope rules

The generic provisioning system must support:

## 4.1 Department-scoped datasets

Visible to every authorized tablet in the department.

Examples:

```text
department_hydrants
department_fire_plans
department_wildfire_operation_maps
```

For department-scoped publications:

```text
station = null
```

## 4.2 Station-scoped datasets

Visible only to tablets whose current vehicle assignment derives the matching station.

Example:

```text
station_personnel
```

For station-scoped publications:

```text
station = required
```

Database and application constraints must enforce the registered scope.

A department-scoped type must reject a station value.

A station-scoped type must reject a missing station value.

---

# 5. Amend DatasetScopeState

Replace the fixed dataset enum with:

```text
DatasetScopeState
- id
- department
- station, nullable
- dataset_type_code
- source_revision
- dirty_since, nullable
- latest_built_publication, nullable
- current_published_publication, nullable
- updated_at
```

Constraints:

- `dataset_type_code` must exist in the code-level registry.
- Scope must match the registered dataset definition.
- One row may exist for each unique combination of:
  - Department
  - Station or null
  - Dataset type code

Suggested uniqueness:

```text
department + station + dataset_type_code
```

Null-safe uniqueness must be enforced appropriately in PostgreSQL.

---

# 6. Amend DatasetPublication

Replace the fixed dataset enum with:

```text
DatasetPublication
- id
- department
- station, nullable
- dataset_type_code
- version_number
- schema_version
- source_revision
- status
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

Requirements:

- `dataset_type_code` must be registered.
- `schema_version` must be valid for the registered type.
- Scope must match the registered definition.
- The publication builder must be selected server-side from the registry.
- Clients must not select a builder, validator, artifact format, or scope.
- Published artifacts remain immutable.

---

# 7. Amend PublicationJob

Use:

```text
PublicationJob
- id
- dataset_type_code
- department
- station, nullable
- source_revision
- status
- requested_by, nullable
- trigger_type
- created_at
- started_at
- heartbeat_at, nullable
- completed_at
- error_message
```

The existing locking, revision, obsolescence, and recovery requirements remain unchanged.

The worker must:

1. Resolve the dataset definition from the registry.
2. Validate its department or station scope.
3. Invoke the registered builder service.
4. Validate the generated plaintext artifact.
5. Encrypt it through the common publication-encryption service.
6. Create the draft publication.
7. Verify that the source revision is still current.

An unknown dataset type must cause the job to fail closed.

---

# 8. Department feature enablement

Add:

```text
DepartmentFeature
- id
- department
- feature_code
- enabled
- enabled_at
- enabled_by
- disabled_at, nullable
- disabled_by, nullable
```

Requirements:

- `feature_code` must correspond to a code-defined feature.
- Administrators cannot enter arbitrary feature codes.
- A feature may enable one or more dataset types.
- Feature enablement does not automatically publish data.
- Disabling a feature removes its dataset from future tablet manifests.
- Disabling a feature does not delete historical publications automatically.
- Enablement and disablement must be audited.

Initial built-in features may include:

```text
core_hydrants
core_fire_plans
core_personnel
```

A future feature may include:

```text
wildfire_operation_maps
```

For the beta, the three core features may be enabled automatically for all departments.

---

# 9. Manifest extensibility

The existing manifest endpoint remains:

```http
GET /api/v1/tablet/manifest
```

Each dataset entry must include:

```json
{
  "publication_id": "uuid",
  "type": "department_wildfire_operation_maps",
  "scope": "department",
  "version": 3,
  "schema_version": 1,
  "minimum_app_version": "1.4.0",
  "artifact_format": "zip",
  "encrypted_size": 1234567,
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
```

The manifest signature must continue to cover:

- Dataset type code
- Scope
- Schema version
- Minimum app version
- Artifact format
- Publication UUID
- Version
- Ciphertext hash
- Encryption metadata
- Installation-specific HPKE grant

---

# 10. Unsupported dataset behavior

The iOS application must safely handle unknown dataset types.

An older app receiving an unknown dataset type must:

- Not download it.
- Not attempt to decrypt or import it.
- Continue processing supported datasets.
- Record a local diagnostic event.
- Display an application-update requirement only when the dataset is marked mandatory.

Add manifest metadata:

```json
{
  "required": false,
  "minimum_app_version": "1.4.0"
}
```

Rules:

- Optional unsupported datasets may be skipped.
- A required dataset with an unsupported schema or unmet minimum app version must cause the tablet to report a configuration incompatibility.
- Unsupported data must not invalidate otherwise supported publications unless explicitly marked required.
- The backend must not send a dataset to an app version that is known to be incompatible when compatibility can be determined server-side.

---

# 11. Dataset schema compatibility

Each dataset type must have an independently versioned schema.

The tablet must not infer compatibility from the publication version.

Example:

```text
Publication version: 18
Schema version: 2
```

Publication version identifies content updates.

Schema version identifies the structure and interpretation of that content.

Each registered type must define:

- Current schema version
- Supported previous schema versions, if any
- Minimum tablet app version
- Whether schema migration on the tablet is supported
- Whether an incompatible change requires a complete replacement

Breaking schema changes must:

- Increment `schema_version`.
- Define a minimum app version.
- Include interoperability tests.
- Be documented in the OpenAPI or dataset-format documentation.

---

# 12. Generic publication services

The following functions must remain generic:

```text
Dataset authorization
Dataset scope resolution
Draft and publication lifecycle
AES-256-GCM artifact encryption
Server-side CEK storage
HPKE key-grant generation
Manifest signing
Protected download authorization
ETag generation
Audit logging
Backup
Rollback
```

The following functions are dataset-specific:

```text
Canonical-data selection
Input validation
Artifact construction
Artifact-format validation
Tablet-side import
Tablet-side presentation
```

Dataset-specific builders must not implement their own:

- Authentication
- Tablet authorization
- Encryption
- HPKE key wrapping
- Download endpoint
- Manifest signature
- File-path construction

They must call the common services.

---

# 13. Future wildfire-operation-map dataset

A future `Waldbrandeinsatzkarten` feature may be implemented as:

```text
Type code:
department_wildfire_operation_maps

Scope:
department

Possible artifact formats:
- ZIP containing georeferenced files and metadata
- GeoPackage
- MBTiles
- GeoJSON plus raster assets
- Sanitized PDF package
```

The exact artifact format is intentionally deferred.

Before implementation, define:

- Source-data format
- Coordinate-reference system
- Map-layer structure
- Offline rendering requirements
- Maximum package size
- Validation rules
- Update frequency
- Licensing and redistribution restrictions
- Tablet storage requirements
- Supported app and schema versions

The first implementation may use a complete encrypted department snapshot.

Incremental tile or map-layer synchronization may be added later without changing the manifest or authorization model.

---

# 14. Security requirements for new dataset types

Every new dataset type must undergo a security review before enablement.

The review must cover:

- Parser attack surface
- Archive extraction safety
- File-count limits
- Decompressed-size limits
- Path traversal
- Symbolic links
- External references
- Active content
- Resource exhaustion
- Coordinate and geometry validation
- Sensitive-data minimization
- Licensing restrictions
- Tablet storage impact
- Encryption and signing interoperability

ZIP-based builders must reject:

- Absolute paths
- `..` path components
- Symbolic links
- Duplicate filenames
- Excessive file counts
- Excessive expanded size
- Unsupported compression methods

---

# 15. Additional tests

Add the following tests:

1. Registered department-scoped types reject a station.
2. Registered station-scoped types require a station.
3. Unknown dataset type codes are rejected.
4. Administrators cannot create arbitrary dataset types.
5. Tablet clients cannot select dataset type, scope, or builder.
6. A department feature can enable a registered dataset type.
7. Disabling a feature removes it from future manifests.
8. Feature changes are audited.
9. An optional unknown dataset does not prevent supported updates.
10. An unsupported required dataset produces a configuration-incompatibility response.
11. Publication and schema versions are interpreted independently.
12. A new registered department-wide dataset uses the existing encryption pipeline.
13. A new registered dataset uses the existing HPKE key-grant mechanism.
14. A department-wide dataset is visible to every active authorized tablet in that department.
15. A department-wide dataset is never visible to another department.
16. A dataset builder cannot control the protected download path.
17. ZIP-based builders reject traversal paths and unsafe archive entries.
18. Unknown registry entries cause publication jobs to fail closed.

---

# 16. Acceptance-criteria additions

The MVP architecture is considered extensible when:

- Dataset types are identified by registry-validated string codes rather than a closed database enum.
- New dataset types can reuse the existing publication, encryption, HPKE, manifest, and download services.
- Administrators cannot define executable or arbitrary dataset behavior.
- Department- and station-scoped datasets are enforced generically.
- Each dataset type has an independent schema version.
- Older tablet applications safely ignore optional unsupported datasets.
- Required incompatible datasets produce a clear update requirement.
- Department feature enablement is explicit and audited.
- A future department-wide wildfire-map dataset can be added without redesigning tablet adoption, authorization, encryption, manifests, or protected downloads.

---

# 17. Implementation-order addition

Add the following after the core publication pipeline:

## Extensible dataset foundation

- Replace fixed dataset-type model choices with registry-validated codes.
- Implement the code-level dataset registry.
- Add generic scope validation.
- Add independent schema-version metadata.
- Add optional and required dataset compatibility handling.
- Add department feature enablement.
- Refactor the three initial dataset builders to use the common builder interface.
- Add tests proving that a fourth department-wide dataset can use the existing pipeline.

This work should be completed during the beta implementation because it is small and prevents unnecessary schema redesign later.

The actual `Waldbrandeinsatzkarten` importer, package format, administrative interface, and tablet renderer remain post-beta work.