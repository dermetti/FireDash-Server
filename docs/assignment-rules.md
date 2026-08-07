# Phase 3 Assignment Rules

`valid_from` is inclusive: an assignment becomes effective at that timestamp. `valid_until` is exclusive: it is the first instant at which the assignment is no longer effective. A null `valid_until` means no scheduled end. `ended_at` records when a domain service actually closed the historical row; it is null while the row remains open.

An assignment is current only when it is not ended and has no elapsed effective end. Phase 3 services create, transfer, and end assignments; Person and Tablet have no management UI or direct writable forms until their later phases.

Management scope determines whether an administrator can administer a department relationship. Operational scope additionally requires an active department, station, and resource. Deactivation is rejected when it would leave an active dependent assignment, except Person and Tablet deactivation services, which atomically close their current assignment rows first.

Every active Person must have exactly one current HOME assignment. `create_person_with_home` creates both atomically, and `transfer_home` closes the old HOME row before creating the replacement and revalidating the invariant.
