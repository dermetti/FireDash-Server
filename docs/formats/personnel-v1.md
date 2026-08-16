# Personnel v1

CSV and JSON array imports are UTF-8 (BOM accepted) and limited to 20 MiB/20,000 rows. Every row/object has exactly `personnel_number`, `first_name`, `last_name`, `incident_commander_eligible`. The first three are required strings; `personnel_number` is the stable canonical identity within a department. The eligibility value is JSON `true`/`false`, or CSV `true`/`false` (empty is treated as false). Dates, station columns, and alternative boolean spellings are not accepted in v1.

New people require the selected active home station in the importing department. Updates retain existing assignments. These imports are add/update only: absence never offboards/deactivates a person or ends an assignment. Those are explicit lifecycle operations.
