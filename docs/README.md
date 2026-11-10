# FireDash documentation

This directory describes the current system. The guides below are authoritative
for their respective subjects. When prose conflicts with code,
migrations, deployed unit files, tests, or the generated OpenAPI schema, those
artifacts are authoritative.

- [Architecture](architecture.md) — product boundary, domain model, workers, and cryptography.
- [Administrator guide](administrator-guide.md) — departmental administration and tablet workflows.
- [Deployment](deployment.md) — supported host, installation, PostgreSQL, systemd, and verifier.
- [Configuration](configuration.md) — runtime settings, credentials, and defaults.
- [Operations](operations.md) — health checks, queues, timers, backup, restore, and troubleshooting.
- [Development](development.md) — local setup, tests, and quality checks.
- [Security](security.md) — trust boundaries, secret handling, crypto, and incident response.
- [Report Transfer](report-transfer.md) - report format, lifecycle, portal, downloads and retention.
- [Dataset ingestion](dataset-ingest.md) - canonical reference-data preview and confirmation workflow.
- [File libraries](file-libraries.md) - source-library permissions, quotas and publication behavior.
- [Tablet API](tablet-api.md) — supported `/api/v1` provisioning, datasets, cryptographic verification and reporting wire contracts.
- [Acceptance checklist](acceptance.md) — production-like evidence still required.

Versioned [format specifications](formats/) retain their named format contracts;
use the Tablet API for the current beta interface and document-generation v2.
UI guides, publication hardening reports and Document Generation v2 historical
acceptance/rollout guides are retained for the next consolidation pass.
