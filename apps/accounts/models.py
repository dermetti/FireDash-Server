import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from apps.accounts.managers import UserManager


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    mfa_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["display_name"]

    class Meta:
        ordering = ["email"]

    def __str__(self) -> str:
        return self.email


class AccountSetupToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="setup_tokens"
    )
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_setup_tokens"
    )
    used_at = models.DateTimeField(null=True, blank=True)

    def is_usable(self, now) -> bool:
        return self.used_at is None and self.expires_at > now


class AuthenticationThrottle(models.Model):
    class Scope(models.TextChoices):
        PASSWORD = "PASSWORD", "Password login"
        MFA = "MFA", "MFA verification"
        SETUP = "SETUP", "Account setup"

    class Subject(models.TextChoices):
        ACCOUNT = "ACCOUNT", "Account"
        IP = "IP", "IP address"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.CharField(max_length=16, choices=Scope.choices)
    subject = models.CharField(max_length=16, choices=Subject.choices)
    subject_hash = models.CharField(max_length=64)
    failure_count = models.PositiveIntegerField(default=0)
    window_started_at = models.DateTimeField()
    locked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("scope", "subject", "subject_hash"), name="unique_auth_throttle_subject"
            )
        ]
