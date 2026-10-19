import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from apps.accounts.models import AccountSetupToken, AuthenticationThrottle


class DuplicateEmailError(ValueError):
    def __init__(self) -> None:
        super().__init__("An account with this email address already exists.")


def validate_new_account_email(email: str) -> str:
    user_model = get_user_model()
    email = user_model.objects.normalize_email(email).casefold()
    if user_model.objects.filter(email=email).exists():
        raise DuplicateEmailError
    return email


def permanently_deactivate_and_anonymize_user(*, user) -> None:
    """Retain an audited identity while removing reusable personal/account data."""
    now = timezone.now()
    user.email = f"deleted-{secrets.token_hex(16)}@anonymized.invalid"
    user.display_name = "Deleted administrator"
    user.is_active = False
    user.mfa_enabled = False
    user.set_unusable_password()
    user.save(update_fields=("email", "display_name", "is_active", "mfa_enabled", "password"))
    AccountSetupToken.objects.filter(user=user, used_at__isnull=True).update(used_at=now)


def _hash(value: str) -> str:
    return salted_hmac("fire-backend-auth", value).hexdigest()


def create_setup_token_for_user(*, user, actor) -> tuple[AccountSetupToken, str]:
    raw_token = secrets.token_urlsafe(32)
    token = AccountSetupToken.objects.create(
        user=user,
        token_hash=_hash(raw_token),
        expires_at=timezone.now() + timedelta(hours=24),
        created_by=actor,
    )
    return token, raw_token


def create_setup_token(*, actor, email: str, display_name: str) -> tuple[AccountSetupToken, str]:
    user_model = get_user_model()
    email = validate_new_account_email(email)
    try:
        with transaction.atomic():
            user = user_model.objects.create_user(email, display_name)
            user.set_unusable_password()
            user.is_active = False
            user.save(update_fields=("password", "is_active"))
            token, raw_token = create_setup_token_for_user(user=user, actor=actor)
    except IntegrityError as error:
        # A competing insert can win after validation. Roll back the savepoint
        # before translating only the email uniqueness violation.
        if getattr(getattr(error.__cause__, "diag", None), "constraint_name", None) == (
            "accounts_user_email_key"
        ):
            raise DuplicateEmailError from error
        raise
    return token, raw_token


def consume_setup_token(*, raw_token: str, password: str):
    token_hash = _hash(raw_token)
    with transaction.atomic():
        token = (
            AccountSetupToken.objects.select_for_update()
            .select_related("user")
            .filter(token_hash=token_hash)
            .first()
        )
        if (
            token is None
            or not constant_time_compare(token.token_hash, token_hash)
            or not token.is_usable(timezone.now())
        ):
            return None
        token.user.set_password(password)
        token.user.is_active = True
        token.user.save(update_fields=("password", "is_active"))
        token.used_at = timezone.now()
        token.save(update_fields=("used_at",))
        return token.user


def is_throttled(*, scope: str, account: str, source_ip: str | None) -> bool:
    now = timezone.now()
    hashes = [_hash(account.casefold())]
    if source_ip:
        hashes.append(_hash(source_ip))
    return AuthenticationThrottle.objects.filter(
        scope=scope,
        subject_hash__in=hashes,
        locked_until__gt=now,
    ).exists()


def record_auth_failure(*, scope: str, account: str, source_ip: str | None) -> None:
    subjects = [(AuthenticationThrottle.Subject.ACCOUNT, account.casefold())]
    if source_ip:
        subjects.append((AuthenticationThrottle.Subject.IP, source_ip))
    now = timezone.now()
    for subject, value in subjects:
        with transaction.atomic():
            throttle, _ = AuthenticationThrottle.objects.select_for_update().get_or_create(
                scope=scope,
                subject=subject,
                subject_hash=_hash(value),
                defaults={"window_started_at": now},
            )
            if (
                throttle.window_started_at
                + timedelta(seconds=settings.AUTH_THROTTLE_WINDOW_SECONDS)
                <= now
            ):
                throttle.failure_count = 0
                throttle.window_started_at = now
                throttle.locked_until = None
            throttle.failure_count += 1
            if throttle.failure_count >= settings.AUTH_THROTTLE_MAX_FAILURES:
                throttle.locked_until = now + timedelta(
                    seconds=settings.AUTH_THROTTLE_LOCKOUT_SECONDS
                )
            throttle.save(
                update_fields=("failure_count", "window_started_at", "locked_until", "updated_at")
            )


def clear_pre_mfa_session(session) -> None:
    for key in ("pending_mfa_user_id", "pending_mfa_started_at"):
        session.pop(key, None)


def pending_mfa_user(session):
    user_id = session.get("pending_mfa_user_id")
    started_at = session.get("pending_mfa_started_at")
    if (
        not user_id
        or not started_at
        or timezone.now().timestamp() - started_at > settings.PRE_MFA_SESSION_MAX_AGE_SECONDS
    ):
        clear_pre_mfa_session(session)
        return None
    return get_user_model().objects.filter(id=user_id, is_active=True).first()
