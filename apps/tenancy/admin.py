"""Django admin para Tenancy.

OrganizationDataSource oculta `credentials_encrypted` por segurança —
edição via management command ou form customizado, não no admin direto.
"""

from __future__ import annotations

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from simple_history.admin import SimpleHistoryAdmin

from .forms import AccessGroupForm, DataSourceCredentialsForm
from .models import (
    AccessGroup,
    Organization,
    OrganizationDataSource,
    OrganizationInvite,
    OrganizationMembership,
    User,
)


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


@admin.register(AccessGroup)
class AccessGroupAdmin(SimpleHistoryAdmin):
    """Grupo de permissões: nome + abas liberadas (checkboxes)."""

    form = AccessGroupForm
    list_display = ("name", "organization", "n_abas", "updated_at")
    list_filter = ("organization",)
    search_fields = ("name", "organization__slug")
    autocomplete_fields = ("organization",)

    @admin.display(description="Abas")
    def n_abas(self, obj: AccessGroup) -> int:
        return len(obj.allowed_pages or [])


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(SimpleHistoryAdmin):
    list_display = (
        "user", "organization", "role", "access_group", "is_active",
        "invited_at", "accepted_at",
    )
    list_filter = ("role", "is_active", "organization", "access_group")
    list_editable = ("access_group",)
    search_fields = ("user__email", "organization__slug")
    autocomplete_fields = ("user", "organization", "access_group")


@admin.register(OrganizationInvite)
class OrganizationInviteAdmin(admin.ModelAdmin):
    list_display = ("email", "organization", "role", "access_group", "invited_by", "created_at")
    list_filter = ("organization", "role")
    search_fields = ("email", "organization__slug")
    autocomplete_fields = ("organization", "access_group", "invited_by", "user")
    readonly_fields = ("created_at",)


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
