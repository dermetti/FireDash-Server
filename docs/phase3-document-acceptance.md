# Owner-controlled Document v2 release acceptance

Local implementation and tests do not authorize a deployment or live verification.
The existing ACTIVE Fake-iPad installation, P-256 key, credentials and verification
history must remain intact. Do not adopt, reset, or delete its state directory.

1. Review the server/client release and deployment order together. Phase 2
   migrations `0032–0033` have not yet been applied to the test server. Before
   schema removal, follow the separately authorized
   [database and ciphertext inventory procedure](document-generation-v2-operations.md).
   Inventory obsolete Fire Plan records, signed generations, artifact paths and
   scope pointers privately. Decide controlled disposition/rebuild of disposable
   publications before dropping their model tables. Never rewrite signed manifests.
2. In a separately authorized maintenance window, coordinate worker shutdown,
   server code deployment, forward migrations and worker restart. Confirm the
   final generic tables and readiness guards are present. Preserve the existing
   installation, assignments and keys. Review the exact commands operationally;
   this checklist does not execute them.
3. Build and publish fresh complete Fire Plan schema-2 generations through the
   normal generic worker. Confirm all four document datasets advertise
   `document-manifest-v2`, use `/document-generations/`, and have READY
   installation-specific generic grants. Do not publish old schema-1 descriptors.
   Dispose of old database records/ciphertext only under separately reviewed,
   explicit authorization; never glob-delete the shared document storage tree.
4. Install the canonical Fake-iPad client code while retaining its state. After
   separately authorizing live acceptance, the owner runs from the normal workspace:

   ```powershell
   .\.venv\Scripts\python.exe -m tools.fake_ipad verify
   ```

5. Confirm signature, HPKE, key unwrap, ciphertext and plaintext integrity PASS,
   complete document membership, correct optional/required flags and conditional
   request results. If source data is unchanged, the historical expectation is
   nine dataset types including 61 Fire Plan PDFs. Current signed contents are
   authoritative; the client contains no historical count requirement.

The nine datasets are Dangerous Goods; department Files, Fire Plans, Hydrants,
KLGV Plans and Phonebook; station Files, Personnel and Phonebook. Dangerous Goods
and the other JSON/GeoJSON datasets retain schema-1 dataset-key-grant delivery.

The main manifest's pending response is problem JSON. A document generation's
pending response is an empty 202 with `Retry-After: 5`; generation manifests have
no conditional 304 contract. Ciphertext conditional 304 is accepted only after
the corresponding bytes have passed size/hash verification in that verification
run. Completed snapshot reuse is reported separately from new downloads.
Document plaintext stays in memory; `--save-plaintext` retains its existing
JSON/GeoJSON behavior. See the [canonical wire contract](tablet-api.md#shared-document-generation-v2-contract).

No live server, installation, deployment or ciphertext-cleanup operations were
performed during Phase 3 development. Live acceptance remains owner-controlled.
