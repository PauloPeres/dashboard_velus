"""Admin de Sync — visibilidade pro Paulo de quando rodou, sucesso/falha, contagens."""

from __future__ import annotations

from django.contrib import admin

from .models import SyncCheckpoint, SyncJob


@admin.register(SyncJob)
class SyncJobAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "source_type",
        "capability",
        "mode",
        "status",
        "records_processed",
        "started_at",
        "finished_at",
    )
    list_filter = ("status", "source_type", "capability", "mode")
    search_fields = ("organization__slug", "error_message")
    readonly_fields = (
        "organization", "source_type", "capability", "mode",
        "status", "started_at", "finished_at", "records_processed",
        "error_message", "created_at", "updated_at",
    )

    def has_add_permission(self, request, obj=None):  # noqa: ARG002
        # Jobs criados pela task, não pelo admin
        return False


@admin.register(SyncCheckpoint)
class SyncCheckpointAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "source_type",
        "capability",
        "last_processed_at",
        "last_external_id",
        "updated_at",
    )
    list_filter = ("source_type", "capability")
    search_fields = ("organization__slug",)
    readonly_fields = ("updated_at",)
