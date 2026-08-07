# Personnel Retention

Each department must configure a positive personnel retention period before a department administrator can offboard a Person. Offboarding closes every current assignment, removes commander eligibility and email data, sets `DEPARTED`, and calculates `retention_until`.

After that timestamp, a recently reauthenticated department administrator may anonymize the record. Anonymization removes names, personnel number, and email data, retains the stable UUID and non-identifying historical assignment times, and sets the display name to `Former member`. Hard deletion is intentionally not implemented.
