# File publication logical-revert reconciliation

File publication comparisons now exclude the source revision counter and internal
storage content IDs. Stable library/folder/document identities, names, hierarchy,
sanitized PDF SHA-256 and sizes remain significant. Frozen source snapshots still
retain every field used by pinning, approval and worker validation.

Only a match with the **active** publication is redundant. A match with a
superseded generation still requires a new version and tablet update; eligible
document artifacts can be reused by the existing document builder.

## Deployment

Apply migrations with the normal deployment procedure before restarting workers.
`publications.0029_publicationjob_allow_clean_rebuild` adds a durable flag for the
existing `allow_clean_rebuild` service option. Legacy un-debounced explicit
USER_REQUEST/BULK_REQUEST jobs are conservatively preserved as force requests.
Reconciliation never bypasses capacity approval for those requests.

No fingerprint backfill of immutable publications is required. Comparisons derive
a logical fingerprint from the retained active snapshot. When it matches, the
scope stores the active publication's original fingerprint as a compatibility
alias, keeping existing SQL state filters and batched read models consistent.
Missing/incomplete historical snapshots remain conservatively dirty and are
reported for investigation; equality is never inferred from counts or revisions.

## Existing affected departments

Use the persisted department UUID, verified through your normal read-only
administration tools. For Feuerwehr Hamburg, substitute that department's UUID
below. Run in the deployed application environment with its normal credentials;
do not use test settings.

```console
python manage.py reconcile_file_publications --department <department-uuid>
python manage.py reconcile_file_publications --department <department-uuid> --apply
python manage.py reconcile_file_publications --department <department-uuid>
```

The default is a read-only dry run, including no audit writes. Review each reported
scope before using `--apply`. The command includes department and station file
scopes only within that department. Each apply transaction rechecks the source
under capacity → scope → library locks. It clears dirty state and cancels redundant
pending jobs/STAGED attempts while retaining their version IDs and snapshots.
Unconsumed publication reviews for obsolete source candidates, including unbound
pending/rejected/approved reviews, are superseded with audit events. Decisions are
never changed to approved, consumed approvals/history are preserved, newer and
unrelated candidates are untouched, and source revisions are not reset.

Running builds are reported as deferred. Let them finish or recover through the
normal worker, then rerun the command. Live edits remain serialized by the same
capacity and source locks; rerunning is safe and makes no changes to an already
reconciled scope. Changed scopes are preserved, with their existing capacity
policy (including unknown telemetry, historical reservations and the 20% floor).

Status rendering checks readiness without creating, superseding, binding or
consuming reviews. Source/telemetry mutations, explicit lifecycle operations,
worker admission and the reconciliation command own durable review changes.
