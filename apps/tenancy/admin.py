"""Django admin para Tenancy.

OrganizationDataSource oculta `credentials_encrypted` por segurança —
edição via management command ou form customizado, não no admin direto.
"""

from __future__ import annotations

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from simple_history.admin import SimpleHistoryAdmin

from .forms import DataSourceCredentialsForm
from .models import Organization, OrganizationDataSource, OrganizationMembership, User


@admin.register(Organization)
class OrganizationAdmin(SimpleHistoryAdmin):
    list_display = ("slug", "name", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("slug", "name")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Email como identificador (sem username)."""

    ordering = ("email",)
    list_display = ("email", "first_name", "last_name", "is_staff", "is_active")
    list_filter = ("is_staff", "is_superuser", "is_active")
    search_fields = ("email", "first_name", "last_name")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Pessoal", {"fields": ("first_name", "last_name")}),
        (
            "Permissões",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Datas", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2"),
            },
        ),
    )


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(SimpleHistoryAdmin):
    list_display = ("user", "organization", "role", "is_active", "invited_at", "accepted_at")
    list_filter = ("role", "is_active", "organization")
    search_fields = ("user__email", "organization__slug")
    autocomplete_fields = ("user", "organization")


@admin.register(OrganizationDataSource)
class OrganizationDataSourceAdmin(SimpleHistoryAdmin):
    """Admin com form customizado para edição segura de credenciais."""

    form = DataSourceCredentialsForm
    list_display = ("organization", "source_type", "capability", "priority", "is_active", "updated_at")
    list_filter = ("source_type", "capability", "is_active")
    search_fields = ("organization__slug",)
    autocomplete_fields = ("organization",)
    exclude = ("credentials_encrypted",)
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("organization", "source_type", "capability", "priority", "is_active")}),
        ("Credenciais", {"fields": ("base_url", "user_id", "api_token")}),
        ("Datas", {"fields": ("created_at", "updated_at")}),
    )
