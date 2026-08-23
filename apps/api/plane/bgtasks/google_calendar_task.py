# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

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


@transaction.atomic
def _prepare_reconciliation(connection_id, generation):
    """Commit creation identity before any provider operation can use it."""

    try:
        connection = lock_google_calendar_connection(connection_id)
    except GoogleCalendarConnection.DoesNotExist:
        return "missing"
    connection.workspace_integration = WorkspaceIntegration.objects.select_for_update().get(
        id=connection.workspace_integration_id
    )
    if connection.lifecycle_generation != generation:
        return "stale"
    if (
        connection.desired_state == GoogleCalendarConnection.DesiredState.CONNECTED
        and connection.status == GoogleCalendarConnection.Status.PENDING
        and not connection.calendar_id
        and connection.calendar_operation_id is None
    ):
        connection.calendar_operation_id = uuid4()
        connection.save(update_fields=["calendar_operation_id", "updated_at"])
    return connection.desired_state


@transaction.atomic
def _converge_present(connection, generation):
    connection = lock_google_calendar_connection(connection.id)
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
            if connection.calendar_operation_id is None:
                return _record_present_error(connection, generation, "Google Calendar creation identity is missing")
            calendar_id = client.find_calendar(connection.calendar_operation_id)
            if calendar_id is None:
                if not _owns_generation(
                    connection,
                    generation,
                    desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
                    status=GoogleCalendarConnection.Status.PENDING,
                ):
                    return "stale"
                calendar_id = client.create_calendar(connection.calendar_operation_id)
            connection.calendar_id = calendar_id
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
    connection.calendar_operation_id = None
    connection.save(update_fields=["calendar_operation_id", "updated_at"])
    mark_google_calendar_connection_active(connection.id, generation)
    return "active"


@transaction.atomic
def _delete_absent_calendar(connection, generation):
    connection = lock_google_calendar_connection(connection.id)
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
        status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
    ):
        return "stale"

    connection.workspace_integration = WorkspaceIntegration.objects.select_for_update().get(
        id=connection.workspace_integration_id
    )
    if not connection.calendar_id and connection.calendar_operation_id is None:
        return "ready"

    client = _client_for(connection)
    try:
        if not connection.calendar_id and connection.calendar_operation_id is not None:
            connection.calendar_id = client.find_calendar(connection.calendar_operation_id) or ""
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
        connection.calendar_operation_id = None
        _persist_refreshed_access_token(connection, client)
        connection.save(update_fields=["calendar_id", "calendar_operation_id", "updated_at"])
    except (GoogleCalendarClientError, GoogleCalendarOAuthConfigurationError) as exc:
        _persist_refreshed_access_token(connection, client)
        return _record_cleanup_error(connection, generation, exc)
    return "ready"


@transaction.atomic
def _complete_absent(connection, generation):
    connection = lock_google_calendar_connection(connection.id)
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
        status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
    ):
        return "stale"
    if connection.calendar_id or connection.calendar_operation_id is not None:
        return "pending"

    retain_grant = connection.retain_grant_after_cleanup
    if not retain_grant and not _account_has_other_token_bearing_connection(connection):
        client = _client_for(connection)
        try:
            client.revoke_grant()
        except (GoogleCalendarClientError, GoogleCalendarOAuthConfigurationError) as exc:
            return _record_cleanup_error(connection, generation, exc)
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
        status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
    ):
        return "stale"
    complete_google_calendar_disconnect(connection.id, generation)
    return "disabled" if retain_grant else "disconnected"


@shared_task
def reconcile_google_calendar_connection(connection_id, generation):
    """Converge only one exact durable Calendar lifecycle generation."""

    prepared_state = _prepare_reconciliation(connection_id, generation)
    if prepared_state in {"missing", "stale"}:
        return prepared_state
    try:
        connection = GoogleCalendarConnection.objects.get(id=connection_id)
    except GoogleCalendarConnection.DoesNotExist:
        return "missing"
    if prepared_state == GoogleCalendarConnection.DesiredState.CONNECTED:
        return _converge_present(connection, generation)
    delete_result = _delete_absent_calendar(connection, generation)
    if delete_result != "ready":
        return delete_result
    return _complete_absent(connection, generation)
