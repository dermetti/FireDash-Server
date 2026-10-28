# Document Generation v2 rollout and historical ciphertext

These are manual operations requiring separate owner authorization. No deployment,
server migration, server-record change or historical-file removal was performed
as part of the Phase 2 code implementation. Fake-iPad and tablet changes belong
to Phase 3; preserve the existing ACTIVE installation and its identities.

Before a controlled rollout, back up the database and private artifact storage.
Inventory obsolete Fire Plan publications and their publication-level artifact
paths, plus the old Fire Plan document-artifact IDs and generated paths, before
migration 0032 drops those model tables. Keep that inventory private. Identify
which disposable publications require controlled cleanup or a fresh canonical
build; do not convert or rewrite their signed manifests. In particular, earlier
Fire Plan snapshots containing a ZIP `path` are not parsed by the new adapter.
Plan their disposal/rebuild explicitly, including any current scope pointers.

Apply the normal forward migration chain during the separately authorized
maintenance window. Historical migrations through 0031 are unchanged. Migration
0032 removes only the five obsolete model tables and their two exclusive guards,
renames the durable cleanup queue in place, and removes the scope delivery selector.
Restrictive table drops fail if unexpected dependencies exist. Migration 0033
strengthens the shared readiness predicate without relaxing the generic guards.
Neither migration deletes physical ciphertext or alters installation identities.

Historical files without generic database records are deliberately left alone.
Ordinary maintenance deletes only known generic orphan records and durable queue
entries, or files allocated by its own failed build. A process killed between
physical promotion and compensation can also leave an untracked file; classify
such files during an explicitly authorized storage audit rather than guessing
from directory age alone.

For a later, separately authorized physical cleanup, use the pre-migration
inventory and current generic artifact/reference/cleanup records together.
Verify each exact generated path remains under the private artifact root and
has no live generic artifact or reference before considering deletion. Never
remove a documents directory tree or infer historical ownership from the shared
`documents/{artifact_id}/artifact.bin` layout. Preserve current generic ciphertext
and all pending durable cleanup work. Record and review the exact proposed file
list before executing the owner-authorized cleanup.

After rollout and controlled disposable-data cleanup, build fresh complete
schema-2 generations through the normal worker and verify generic readiness and
authorized delivery. Client consolidation and live acceptance remain Phase 3.
