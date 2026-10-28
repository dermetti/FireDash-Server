# File publication logical-revert reconciliation

File publication comparisons now exclude the source revision counter and internal
storage content IDs. Stable library/folder/document identities, names, hierarchy,
sanitized PDF SHA-256 and sizes remain significant. Frozen source snapshots still
retain every field used by pinning and worker validation.

Only a match with the **active** publication is redundant. A match with a
superseded generation still requires a new version and tablet update; eligible
document artifacts can be reused by the existing document builder.

An authorized explicit **Rebuild** persists `allow_clean_rebuild=True` and publishes
a new generation even when it matches the active source. Build Now and ordinary
bulk requests do not acquire this intent. Device capacity does not affect claim
or finalization.
Timeout recovery retains intent on the job, and replacements for changed source retain it on the new attempt. Device capacity is not evaluated.
Successful activation supersedes the old generation normally; eligible document
artifacts may be reused without reusing the generation's delivery identity.
This finalization correction needs no additional migration. Restart publication
workers with the updated code; already terminal OBSOLETE attempts are retained
and require a new explicit Rebuild rather than automatic resurrection.

## Deployment

Apply migrations with the normal deployment procedure before restarting workers.
`publications.0029_publicationjob_allow_clean_rebuild` adds a durable flag for the
existing `allow_clean_rebuild` service option. Apply
`publications.0030_correct_legacy_bulk_rebuild_intent` as well: 0029 incorrectly
classified un-debounced BULK_REQUEST jobs as force rebuilds. 0029 is retained
unchanged because it may already be deployed. 0030 corrects active bulk jobs
created before 0029 was applied, including RUNNING jobs, without changing their
worker state, snapshot or version. Each correction appends a
`publication.legacy_bulk_intent_corrected` audit event. It is idempotent.

Explicit USER_REQUEST jobs retain 0029's classification: jobs with a debounce
origin (including Build Now promotions) remain ordinary updates. Bulk jobs with
a `publication.rebuild_requested` audit event matching the exact department,
station, scope and dataset since the previous terminal job retain force protection.
This preserves requests subsequently coalesced into a bulk job, even if its
requester or revision changed. Jobs created
after 0029 are untouched, since their explicit flag is authoritative. New bulk
request semantics are unchanged.

Legacy bulk intent cannot be reconstructed perfectly: a direct internal force
enqueue without a rebuild audit event is indistinguishable from 0029's erroneous
backfill. Such legacy bulk jobs are classified as ordinary bulk work. Conversely,
an ambiguous older explicit request since the previous job's terminal timestamp
(or creation time when completion is unavailable) conservatively retains
protection. Review the correction audit events and any protected legacy
jobs when reconciling. No review decision is approved or consumed by migration.
Running builds finish or recover normally before pending clean-source work can
be reconciled. Explicit rebuild intent remains independent of device capacity.

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
under department source → scope → library locks. It clears dirty state and cancels redundant
pending jobs/STAGED attempts while retaining their version IDs and snapshots.
Historical capacity reviews and audit records remain unchanged. Source revisions are not reset.

Running builds are reported as deferred. Let them finish or recover through the
normal worker, then rerun the command. Live edits remain serialized by the same
department source and scope locks; rerunning is safe and makes no changes to an already
reconciled scope. Changed scopes remain queued under the normal publication policy.
Historical capacity reviews and measurements are inert.
