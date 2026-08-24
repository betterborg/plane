# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from plane.db.models import BaseModel
from plane.license.utils.encryption import derive_key


class EncryptedTextField(models.TextField):
    """A text field whose non-empty values are encrypted at rest."""

    encrypted_prefix = "fernet$"

    def _cipher(self):
        return Fernet(derive_key(settings.SECRET_KEY))

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        if not value.startswith(self.encrypted_prefix):
            raise ValidationError("Encrypted field contains an unsupported value")
        try:
            return self._cipher().decrypt(value.removeprefix(self.encrypted_prefix).encode()).decode()
        except InvalidToken as exc:
            raise ValidationError("Encrypted field could not be decrypted") from exc

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        return f"{self.encrypted_prefix}{self._cipher().encrypt(value.encode()).decode()}"


class GoogleCalendarConnection(BaseModel):
    """A workspace member's durable Google Calendar grant and OAuth attempt state."""

    class DesiredState(models.TextChoices):
        DISCONNECTED = "disconnected", "Disconnected"
        CONNECTED = "connected", "Connected"

    class Status(models.TextChoices):
        DISCONNECTED = "disconnected", "Disconnected"
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        ERROR = "error", "Error"
        CLEANUP_PENDING = "cleanup_pending", "Cleanup pending"

    workspace_integration = models.ForeignKey(
        "db.WorkspaceIntegration", related_name="google_calendar_connections", on_delete=models.CASCADE
    )
    member = models.ForeignKey("db.User", related_name="google_calendar_connections", on_delete=models.CASCADE)

    # Provider binding. Tokens are transparently encrypted before persistence.
    provider_account_id = models.CharField(max_length=255, blank=True)
    provider_email = models.EmailField(blank=True)
    calendar_id = models.CharField(max_length=255, blank=True)
    calendar_operation_id = models.UUIDField(null=True, blank=True)
    access_token = EncryptedTextField(blank=True)
    refresh_token = EncryptedTextField(blank=True)
    sync_token = EncryptedTextField(blank=True)
    page_token = EncryptedTextField(blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.JSONField(default=list, blank=True)
    credential_fingerprint = models.CharField(max_length=64, blank=True)

    # Inventory state is private worker custody and must never be serialized.
    reconciliation_lease_expires_at = models.DateTimeField(null=True, blank=True)
    reconciliation_completed_at = models.DateTimeField(null=True, blank=True)
    reconciliation_phase = models.CharField(max_length=32, blank=True)
    reconciliation_cursor = models.TextField(blank=True)
    calendar_generation = models.PositiveBigIntegerField(default=0)

    # Durable lifecycle state. OAuth attempts must not mutate these fields.
    desired_state = models.CharField(max_length=32, choices=DesiredState.choices, default=DesiredState.DISCONNECTED)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DISCONNECTED)
    lifecycle_generation = models.PositiveBigIntegerField(default=0)
    retain_grant_after_cleanup = models.BooleanField(default=False)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    broken_notified_at = models.DateTimeField(null=True, blank=True)
    broken_email_sent_at = models.DateTimeField(null=True, blank=True)

    # Short-lived OAuth attempt state is kept separate from the durable lifecycle.
    oauth_state = models.CharField(max_length=255, blank=True)
    oauth_code_verifier = models.TextField(blank=True)
    oauth_redirect_uri = models.TextField(blank=True)
    oauth_attempt_expires_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.member_id} <{self.workspace_integration_id}>"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("workspace_integration", "member"),
                name="google_calendar_connection_unique_member",
            )
        ]
        verbose_name = "Google Calendar Connection"
        verbose_name_plural = "Google Calendar Connections"
        db_table = "google_calendar_connections"
        ordering = ("-created_at",)


class GoogleCalendarEvent(BaseModel):
    """A durable correlation between a Plane entity and a Google Calendar event."""

    class EntityType(models.TextChoices):
        WORK_ITEM = "work_item", "Work item"
        CYCLE = "cycle", "Cycle"

    connection = models.ForeignKey(
        GoogleCalendarConnection,
        related_name="events",
        on_delete=models.CASCADE,
    )
    entity_type = models.CharField(max_length=32, choices=EntityType.choices)
    entity_id = models.UUIDField()
    google_event_id = models.CharField(max_length=1024)
    payload_hash = models.CharField(max_length=64)
    calendar_generation = models.PositiveBigIntegerField(default=0)
    provider_etag = models.CharField(max_length=1024, blank=True)
    provider_payload_hash = models.CharField(max_length=64, blank=True)
    provider_status = models.CharField(max_length=32, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("connection", "entity_type", "entity_id"),
                name="google_calendar_event_unique_entity",
            ),
            models.UniqueConstraint(
                fields=("connection", "google_event_id"),
                name="google_calendar_event_unique_google_event",
            ),
        ]
        verbose_name = "Google Calendar Event"
        verbose_name_plural = "Google Calendar Events"
        db_table = "google_calendar_events"
        ordering = ("-created_at",)
