import uuid

from django.db import models


class ReportSubmission(models.Model):
    installation = models.ForeignKey("tablets.AppInstallation", on_delete=models.PROTECT)
    submission_id = models.UUIDField()
    report_id = models.UUIDField()
    recipient = models.ForeignKey("personnel.Person", on_delete=models.PROTECT)
    department = models.ForeignKey("organizations.Department", on_delete=models.PROTECT)
    station = models.ForeignKey("organizations.Station", on_delete=models.PROTECT)
    payload_digest = models.CharField(max_length=64)
    # Blank/null identifies pre-ReportDocument development packages; no invented backfill.
    document_digest = models.CharField(max_length=64, blank=True)
    document_schema_version = models.PositiveSmallIntegerField(null=True)
    first_download_at = models.DateTimeField(null=True)
    payload_key = models.CharField(max_length=64, blank=True)
    key_version = models.CharField(max_length=64)
    state = models.CharField(
        max_length=16,
        default="READY",
        choices=[
            ("READY", "Ready"),
            ("EXPIRED", "Expired"),
        ],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("installation", "report_id"), name="report_installation_report_identity"
            ),
            models.UniqueConstraint(
                fields=("installation", "submission_id"),
                name="report_submission_installation_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=("READY", "EXPIRED")),
                name="report_submission_state_valid",
            ),
        ]


class ReportTransfer(models.Model):
    """One immutable access generation; never stores a bearer token or email."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey("tablets.AppInstallation", on_delete=models.PROTECT)
    recipient = models.ForeignKey("personnel.Person", on_delete=models.PROTECT)
    transfer_request_id = models.UUIDField()
    request_digest = models.CharField(max_length=64)
    recipient_email_digest = models.CharField(max_length=64)
    submissions = models.ManyToManyField(ReportSubmission)
    token_digest = models.CharField(max_length=64, unique=True)
    issued_at = models.DateTimeField()
    token_expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True)
    revoked_at = models.DateTimeField(null=True)
    mail_state = models.CharField(max_length=16, default="PROCESSING")
    mail_code = models.CharField(max_length=40, default="pending")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("installation", "transfer_request_id"),
                name="report_transfer_request_identity",
            ),
            models.UniqueConstraint(
                fields=("installation", "recipient"),
                condition=models.Q(revoked_at__isnull=True),
                name="report_transfer_active_generation",
            ),
            models.CheckConstraint(
                condition=models.Q(mail_state__in=("PROCESSING", "SENT", "FAILED", "UNKNOWN")),
                name="report_transfer_mail_state_valid",
            ),
        ]


class ReportPortalSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transfer = models.ForeignKey(ReportTransfer, on_delete=models.CASCADE)
    session_digest = models.CharField(max_length=64)
    last_seen_at = models.DateTimeField()
    logged_out_at = models.DateTimeField(null=True)
