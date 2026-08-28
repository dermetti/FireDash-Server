# Personnel v1

CSV imports are UTF-8 (BOM accepted) and limited to 20 MiB/20,000 rows. The exact header is `personnel_number,first_name,last_name,home_station,incident_commander_eligible`. The first three values are required strings; `personnel_number` is the stable Department-scoped identity used for upsert matching. `incident_commander_eligible` is CSV `true`/`false` (empty is treated as false). Import mode is selected on the upload page, never in CSV.

`home_station` accepts an active same-Department Station Short Code or full Station name. It is required for new Personnel; an existing Person may leave it blank to retain their current HOME assignment. Ambiguous references require an explicit same-Department review choice. Missing references never create a Station: correct the CSV or create the Station through Import Stations and Vehicles, then retry. Preview/review remains staged until explicit final Apply. Omitted rows never offboard/deactivate Personnel or end assignments.

Final Apply is atomic. A stale preview, unresolved/ambiguous station decision,
or validation/apply failure leaves Personnel and HOME assignments unchanged;
the importer never partially applies a CSV.
