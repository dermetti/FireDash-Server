"""Provider-independent email address helpers."""

from apps.outbound_mail.runtime import MailAddress


def recipient_to_mail_address(email: str) -> MailAddress:
    """Avoid recipient display names; personnel identity is never mail content."""
    return MailAddress(display_name="FireDash recipient", email=email)
