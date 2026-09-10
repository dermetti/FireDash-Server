from django.core.management.base import BaseCommand, CommandError

from apps.outbound_mail.secret_rotation import (
    OutboundMailSecretRotationError,
    outbound_mail_secret_rotation_status,
    rotate_outbound_mail_secrets,
)


class Command(BaseCommand):
    help = "Inspect, rotate, or check retirement readiness for outbound-mail encrypted credentials."

    def add_arguments(self, parser) -> None:
        actions = parser.add_mutually_exclusive_group(required=True)
        actions.add_argument(
            "--status", action="store_true", help="Inspect envelope versions without writes."
        )
        actions.add_argument(
            "--rotate", action="store_true", help="Re-encrypt older live envelopes."
        )
        actions.add_argument(
            "--check-retirement",
            metavar="VERSION",
            help="Fail unless the version has no live references.",
        )

    def handle(self, *args, **options) -> None:
        if options["status"]:
            status = outbound_mail_secret_rotation_status()
            counts = ", ".join(
                f"{version}={count}" for version, count in status.version_counts.items()
            )
            self.stdout.write(
                f"active_version={status.active_version} live_versions={counts or 'none'} "
                f"all_live_credentials_active={status.all_live_credentials_active}"
            )
            return
        if options["check_retirement"]:
            status = outbound_mail_secret_rotation_status()
            version = options["check_retirement"]
            if not status.can_retire(version):
                raise CommandError(
                    f"Key version {version!r} is still referenced or usage is indeterminate."
                )
            self.stdout.write(
                f"Key version {version!r} has no live outbound-mail credential references."
            )
            return
        try:
            result = rotate_outbound_mail_secrets()
        except OutboundMailSecretRotationError as error:
            raise CommandError(
                f"Rotation stopped at {error.reference} ({error.code}); "
                "correct the deployment/key data and rerun."
            ) from None
        self.stdout.write(
            f"active_version={result.active_version} rotated={result.rotated_count} "
            f"skipped_active={result.skipped_active_count} "
            f"skipped_empty={result.skipped_empty_count}"
        )
