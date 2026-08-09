# Administrator Beta Guide

## Before inviting tablets

Confirm that your administrator account has completed MFA and that you are working in the correct
department. Create stations, vehicles, and personnel before publishing data. Department
administrators can work across their department; station administrators are limited to their
assigned stations and cannot adopt, reactivate, or publish tablets.

Review automatically generated drafts after personnel, hydrant, or fire-plan changes. Publishing
is an explicit department-administrator action. A published package is encrypted once for its
authorized scope; do not distribute files from server storage or attempt to copy packages between
departments or stations.

## Adopt and operate a tablet

1. Create a tablet for the correct vehicle and generate an adoption invitation.
2. Complete adoption only while physically controlling the tablet and confirm the displayed device details.
3. Allow the tablet to check in and retrieve its manifest over the private HTTPS network.
4. Verify that it receives department-wide packages and only its station-specific personnel package.

An authorization lease lasts seven days after a successful check-in. A tablet with an expired
lease cannot receive manifests, grants, or downloads. It cannot reactivate itself. A department
administrator must generate a reactivation invitation, complete the reactivation process with the
tablet, and confirm that its credential was rotated before access resumes.

## Lost, replaced, or suspicious tablets

Remove or revoke a lost, retired, or suspicious tablet immediately. This revokes its active or
stale installation and associated dataset-key grants. Record the operational reason in the
administrative workflow and notify the infrastructure administrator when credential compromise or
unexpected access is suspected. Do not use application screens to investigate incident data: the
system is not an incident-management system and must not receive incident information.

## Beta operating checks

At least weekly, review unpublished drafts, failed publication jobs, stale tablets, and the
department audit trail. Escalate failed backups, repeated adoption/check-in failures, and access
denials to the infrastructure administrator. Administrators must never request or handle server
keys, backup passwords, restic credentials, database passwords, or raw encrypted artifacts.
