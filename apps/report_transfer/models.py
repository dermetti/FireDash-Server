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
