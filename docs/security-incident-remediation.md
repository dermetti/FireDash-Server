# Secret Remediation Procedure

If a credential is found in the workspace or repository, treat it as exposed.

1. Disable or rotate the credential through the owning system before changing the
   repository. Rotation is an operational action and must not place replacement
   material in this repository.
2. Remove the file from the working tree and repository history using the
   organization's approved incident process.
3. Invalidate affected sessions, backup jobs, or service credentials as appropriate.
4. Record the rotation and validation in the restricted security incident record,
   not in application logs or source control.
5. Verify `.gitignore`, pre-commit or CI secret scanning, and deployment credential
   paths prevent recurrence.

This workspace does not contain replacement secrets. Production credentials belong
in root-owned systemd credentials, the approved secrets manager, or the protected
backup process.
