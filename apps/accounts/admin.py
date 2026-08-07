from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from apps.accounts.models import User


@admin.register(User)
class FireDashUserAdmin(UserAdmin):  # type: ignore[type-arg]
    model = User
    list_display = ("email", "display_name", "is_active", "mfa_enabled")
    ordering = ("email",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Identity", {"fields": ("display_name",)}),
        (
            "Status",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                    "mfa_enabled",
                )
            },
        ),
        ("Important dates", {"fields": ("last_login", "created_at")}),
    )
    readonly_fields = ("created_at",)
    add_fieldsets = (
        (
            None,
            {"classes": ("wide",), "fields": ("email", "display_name", "password1", "password2")},
        ),
    )
    search_fields = ("email", "display_name")
