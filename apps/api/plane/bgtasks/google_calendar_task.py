# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from celery import shared_task
from django.db import transaction
from django.db.models import Q

from plane.db.models import GoogleCalendarConnection, WorkspaceIntegration
from plane.integrations.google_calendar.client import GoogleCalendarClient, GoogleCalendarClientError
from plane.integrations.google_calendar.lifecycle import (
    complete_google_calendar_disconnect,
    has_usable_google_calendar_grant,
    lock_google_calendar_connection,
    mark_google_calendar_connection_active,
    mark_google_calendar_connection_error,
    record_google_calendar_cleanup_error,
)
from plane.integrations.google_calendar.oauth import GoogleCalendarOAuthConfigurationError


def _owns_generation(connection, generation, *, desired_state, status):
    return (
        connection.lifecycle_generation == generation
        and connection.desired_state == desired_state
        and connection.status == status
    )


def _client_for(connection):
    return GoogleCalendarClient(
        access_token=connection.access_token,
        refresh_token=connection.refresh_token,
        token_expires_at=connection.token_expires_at,
    )


def _persist_refreshed_access_token(connection, client):
    access_token = client.access_token
    if access_token is None:
        return
    if connection.access_token == access_token.value and connection.token_expires_at == access_token.expires_at:
        return
    connection.access_token = access_token.value
    connection.token_expires_at = access_token.expires_at
    connection.save(update_fields=["access_token", "token_expires_at", "updated_at"])


def _record_present_error(connection, generation, error):
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
        status=GoogleCalendarConnection.Status.PENDING,
    ):
        return "stale"
    mark_google_calendar_connection_error(connection.id, generation, error)
    return "error"


def _record_cleanup_error(connection, generation, error):
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
        status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
    ):
        return "stale"
    record_google_calendar_cleanup_error(connection.id, generation, error)
    return "error"


def _account_has_other_token_bearing_connection(connection):
    if not connection.provider_account_id:
        return False
    return (
        GoogleCalendarConnection.objects.select_for_update()
        .filter(provider_account_id=connection.provider_account_id)
        .exclude(id=connection.id)
        .filter(Q(access_token__gt="") | Q(refresh_token__gt=""))
        .order_by("id")
        .exists()
    )


def _converge_present(connection, generation):
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
        status=GoogleCalendarConnection.Status.PENDING,
    ):
        return "stale"
    if not has_usable_google_calendar_grant(connection):
        return _record_present_error(connection, generation, "Google Calendar grant is not usable")

    client = _client_for(connection)
    try:
        if not connection.calendar_id:
            if not _owns_generation(
                connection,
                generation,
                desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
                status=GoogleCalendarConnection.Status.PENDING,
            ):
                return "stale"
            connection.calendar_id = client.create_calendar()
            _persist_refreshed_access_token(connection, client)
            if not _owns_generation(
                connection,
                generation,
                desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
                status=GoogleCalendarConnection.Status.PENDING,
            ):
                return "stale"
            connection.save(update_fields=["calendar_id", "updated_at"])
    except (GoogleCalendarClientError, GoogleCalendarOAuthConfigurationError) as exc:
        _persist_refreshed_access_token(connection, client)
        return _record_present_error(connection, generation, exc)

    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
        status=GoogleCalendarConnection.Status.PENDING,
    ):
        return "stale"
    mark_google_calendar_connection_active(connection.id, generation)
    return "active"


def _converge_absent(connection, generation):
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
        status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
    ):
        return "stale"

    retain_grant = not bool(connection.workspace_integration.config.get("enabled", False))
    client = _client_for(connection)
    try:
        if connection.calendar_id:
            if not _owns_generation(
                connection,
                generation,
                desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
                status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            ):
                return "stale"
            recorded_calendar_id = connection.calendar_id
            client.delete_calendar(recorded_calendar_id)
            _persist_refreshed_access_token(connection, client)
            if not _owns_generation(
                connection,
                generation,
                desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
                status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            ):
                return "stale"
            if connection.calendar_id != recorded_calendar_id:
                return "stale"
            connection.calendar_id = ""
            connection.save(update_fields=["calendar_id", "updated_at"])

        if not retain_grant and not _account_has_other_token_bearing_connection(connection):
            if not _owns_generation(
                connection,
                generation,
                desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
                status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            ):
                return "stale"
            client.revoke_grant()
    except (GoogleCalendarClientError, GoogleCalendarOAuthConfigurationError) as exc:
        _persist_refreshed_access_token(connection, client)
        return _record_cleanup_error(connection, generation, exc)

    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
        status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
    ):
        return "stale"
    complete_google_calendar_disconnect(connection.id, generation, retain_grant=retain_grant)
    return "disabled" if retain_grant else "disconnected"


@shared_task
@transaction.atomic
def reconcile_google_calendar_connection(connection_id, generation):
    """Converge only one exact durable Calendar lifecycle generation."""

    try:
        connection = lock_google_calendar_connection(connection_id)
    except GoogleCalendarConnection.DoesNotExist:
        return "missing"
    connection.workspace_integration = WorkspaceIntegration.objects.select_for_update().get(
        id=connection.workspace_integration_id
    )
    if connection.lifecycle_generation != generation:
        return "stale"
    if connection.desired_state == GoogleCalendarConnection.DesiredState.CONNECTED:
        return _converge_present(connection, generation)
    return _converge_absent(connection, generation)
