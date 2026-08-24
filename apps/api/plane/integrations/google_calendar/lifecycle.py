# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import hashlib
from dataclasses import dataclass
from uuid import UUID

from django.db import connection as database_connection
from django.db import transaction
from django.db.transaction import TransactionManagementError
from django.utils import timezone

from plane.db.models import GoogleCalendarConnection


class GoogleCalendarLifecycleError(Exception):
    """Base error for rejected Calendar lifecycle mutations."""


class StaleGoogleCalendarGeneration(GoogleCalendarLifecycleError):
    """Raised when a lifecycle consumer no longer owns the exact generation."""


class IllegalGoogleCalendarTransition(GoogleCalendarLifecycleError):
    """Raised when a durable Calendar state transition is not allowed."""


class UnusableGoogleCalendarGrant(GoogleCalendarLifecycleError):
    """Raised when an OAuth result does not contain a usable provider grant."""


class GoogleCalendarDisableCleanupInProgress(GoogleCalendarLifecycleError):
    """Raised when a workspace is re-enabled before its calendars are removed."""


@dataclass(frozen=True)
class GoogleCalendarLifecycleCommand:
    """Generation-scoped work to publish after a lifecycle transaction commits."""

    connection_id: UUID
    generation: int


_GLOBAL_LOCK_SCOPE = "google-calendar:lifecycle:global"
_CONNECTION_LOCK_SCOPE = "google-calendar:lifecycle:connection"

_LEGAL_TRANSITIONS = {
    (
        GoogleCalendarConnection.DesiredState.DISCONNECTED,
        GoogleCalendarConnection.Status.DISCONNECTED,
    ): {
        (
            GoogleCalendarConnection.DesiredState.DISCONNECTED,
            GoogleCalendarConnection.Status.CLEANUP_PENDING,
        ),
        (
            GoogleCalendarConnection.DesiredState.CONNECTED,
            GoogleCalendarConnection.Status.PENDING,
        ),
    },
    (
        GoogleCalendarConnection.DesiredState.CONNECTED,
        GoogleCalendarConnection.Status.PENDING,
    ): {
        (
            GoogleCalendarConnection.DesiredState.CONNECTED,
            GoogleCalendarConnection.Status.ACTIVE,
        ),
        (
            GoogleCalendarConnection.DesiredState.CONNECTED,
            GoogleCalendarConnection.Status.ERROR,
        ),
        (
            GoogleCalendarConnection.DesiredState.DISCONNECTED,
            GoogleCalendarConnection.Status.CLEANUP_PENDING,
        ),
    },
    (
        GoogleCalendarConnection.DesiredState.CONNECTED,
        GoogleCalendarConnection.Status.ACTIVE,
    ): {
        (
            GoogleCalendarConnection.DesiredState.CONNECTED,
            GoogleCalendarConnection.Status.PENDING,
        ),
        (
            GoogleCalendarConnection.DesiredState.CONNECTED,
            GoogleCalendarConnection.Status.ERROR,
        ),
        (
            GoogleCalendarConnection.DesiredState.DISCONNECTED,
            GoogleCalendarConnection.Status.CLEANUP_PENDING,
        ),
    },
    (
        GoogleCalendarConnection.DesiredState.CONNECTED,
        GoogleCalendarConnection.Status.ERROR,
    ): {
        (
            GoogleCalendarConnection.DesiredState.CONNECTED,
            GoogleCalendarConnection.Status.PENDING,
        ),
        (
            GoogleCalendarConnection.DesiredState.DISCONNECTED,
            GoogleCalendarConnection.Status.CLEANUP_PENDING,
        ),
    },
    (
        GoogleCalendarConnection.DesiredState.DISCONNECTED,
        GoogleCalendarConnection.Status.CLEANUP_PENDING,
    ): {
        (
            GoogleCalendarConnection.DesiredState.DISCONNECTED,
            GoogleCalendarConnection.Status.DISCONNECTED,
        ),
        (
            GoogleCalendarConnection.DesiredState.CONNECTED,
            GoogleCalendarConnection.Status.PENDING,
        ),
    },
}

_PROVIDER_FIELDS = (
    "provider_account_id",
    "provider_email",
    "access_token",
    "refresh_token",
    "sync_token",
    "page_token",
    "token_expires_at",
    "scopes",
    "credential_fingerprint",
)
_OAUTH_ATTEMPT_FIELDS = (
    "oauth_state",
    "oauth_code_verifier",
    "oauth_redirect_uri",
    "oauth_attempt_expires_at",
)


def _stable_advisory_lock_key(scope, identifier=""):
    value = f"{scope}:{identifier}".encode()
    digest = hashlib.blake2b(value, digest_size=8, person=b"plane-gcal-lock").digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _acquire_advisory_xact_lock(lock_key):
    if not database_connection.in_atomic_block:
        raise TransactionManagementError("Google Calendar lifecycle locks require an atomic transaction")
    with database_connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_key])


def _acquire_global_lock():
    _acquire_advisory_xact_lock(_stable_advisory_lock_key(_GLOBAL_LOCK_SCOPE))


def _acquire_connection_lock(connection_id):
    _acquire_advisory_xact_lock(_stable_advisory_lock_key(_CONNECTION_LOCK_SCOPE, connection_id))


def lock_google_calendar_connection(connection_id):
    """Lock one connection using the mandatory global-before-connection order."""

    _acquire_global_lock()
    _acquire_connection_lock(connection_id)
    return GoogleCalendarConnection.objects.select_for_update().get(id=connection_id)


def lock_google_calendar_global_state():
    """Serialize an instance-wide Calendar policy operation with lifecycle work."""

    _acquire_global_lock()


def lock_google_calendar_workspace_connections(workspace_id):
    """Lock a workspace's connections in stable UUID order after the global lock."""

    _acquire_global_lock()
    connection_ids = list(
        GoogleCalendarConnection.objects.filter(workspace_integration__workspace_id=workspace_id)
        .order_by("id")
        .values_list("id", flat=True)
    )
    for connection_id in connection_ids:
        _acquire_connection_lock(connection_id)
    connections = GoogleCalendarConnection.objects.select_for_update().filter(id__in=connection_ids).order_by("id")
    return list(connections)


def lock_all_google_calendar_connections():
    """Lock live and soft-deleted Calendar rows in the global lifecycle order."""

    _acquire_global_lock()
    connection_ids = list(GoogleCalendarConnection.all_objects.order_by("id").values_list("id", flat=True))
    for connection_id in connection_ids:
        _acquire_connection_lock(connection_id)
    connections = GoogleCalendarConnection.all_objects.select_for_update().filter(id__in=connection_ids).order_by("id")
    return list(connections)


def has_usable_google_calendar_grant(connection, at=None):
    """Return whether a durable row has provider identity and a usable token."""

    if not connection.provider_account_id:
        return False
    if connection.refresh_token:
        return True
    checked_at = at or timezone.now()
    return bool(connection.access_token and connection.token_expires_at and connection.token_expires_at > checked_at)


def _assert_generation(connection, expected_generation):
    if connection.lifecycle_generation != expected_generation:
        raise StaleGoogleCalendarGeneration(
            f"Expected generation {expected_generation}, found {connection.lifecycle_generation}"
        )


def _transition(connection, expected_generation, desired_state, status, *, advance_generation=False):
    _assert_generation(connection, expected_generation)
    current_state = (connection.desired_state, connection.status)
    next_state = (desired_state, status)
    if next_state not in _LEGAL_TRANSITIONS.get(current_state, set()):
        raise IllegalGoogleCalendarTransition(
            f"Cannot transition Google Calendar connection from {current_state} to {next_state}"
        )

    connection.desired_state = desired_state
    connection.status = status
    update_fields = ["desired_state", "status", "updated_at"]
    if advance_generation:
        connection.lifecycle_generation = expected_generation + 1
        update_fields.append("lifecycle_generation")
    connection.save(update_fields=update_fields)
    return connection


def clear_google_calendar_oauth_attempt(connection):
    """Clear only short-lived consent-attempt metadata on a locked connection."""

    connection.oauth_state = ""
    connection.oauth_code_verifier = ""
    connection.oauth_redirect_uri = ""
    connection.oauth_attempt_expires_at = None


def _clear_provider_grant(connection):
    connection.provider_account_id = ""
    connection.provider_email = ""
    connection.access_token = ""
    connection.refresh_token = ""
    connection.sync_token = ""
    connection.page_token = ""
    connection.token_expires_at = None
    connection.scopes = []
    connection.credential_fingerprint = ""


@transaction.atomic
def apply_google_calendar_oauth_success(
    connection_id,
    expected_generation,
    *,
    provider_account_id,
    provider_email,
    credential_fingerprint,
    access_token="",
    refresh_token="",
    token_expires_at=None,
    scopes=None,
):
    """Persist a validated OAuth grant and return its new lifecycle command."""

    calendar_connection = lock_google_calendar_connection(connection_id)
    _assert_generation(calendar_connection, expected_generation)
    current_state = (calendar_connection.desired_state, calendar_connection.status)
    oauth_success_state = (
        GoogleCalendarConnection.DesiredState.CONNECTED,
        GoogleCalendarConnection.Status.PENDING,
    )
    allowed_oauth_states = {
        (
            GoogleCalendarConnection.DesiredState.DISCONNECTED,
            GoogleCalendarConnection.Status.DISCONNECTED,
        ),
        (
            GoogleCalendarConnection.DesiredState.CONNECTED,
            GoogleCalendarConnection.Status.PENDING,
        ),
        (
            GoogleCalendarConnection.DesiredState.CONNECTED,
            GoogleCalendarConnection.Status.ACTIVE,
        ),
        (
            GoogleCalendarConnection.DesiredState.CONNECTED,
            GoogleCalendarConnection.Status.ERROR,
        ),
    }
    if current_state not in allowed_oauth_states:
        raise IllegalGoogleCalendarTransition(
            f"Cannot apply OAuth success to Google Calendar connection in {current_state}"
        )

    calendar_connection.provider_account_id = provider_account_id
    calendar_connection.provider_email = provider_email
    calendar_connection.access_token = access_token
    calendar_connection.refresh_token = refresh_token
    calendar_connection.token_expires_at = token_expires_at
    calendar_connection.scopes = list(scopes or [])
    calendar_connection.credential_fingerprint = credential_fingerprint
    calendar_connection.desired_state = GoogleCalendarConnection.DesiredState.CONNECTED
    if not credential_fingerprint or not has_usable_google_calendar_grant(calendar_connection):
        raise UnusableGoogleCalendarGrant(
            "OAuth success requires provider identity, a usable token, and a credential fingerprint"
        )

    calendar_connection.desired_state, calendar_connection.status = oauth_success_state
    calendar_connection.lifecycle_generation = expected_generation + 1
    calendar_connection.retain_grant_after_cleanup = False
    calendar_connection.last_error = ""
    clear_google_calendar_oauth_attempt(calendar_connection)
    calendar_connection.save(
        update_fields=[
            *_PROVIDER_FIELDS,
            "desired_state",
            "status",
            "lifecycle_generation",
            "retain_grant_after_cleanup",
            "last_error",
            *_OAUTH_ATTEMPT_FIELDS,
            "updated_at",
        ]
    )
    return GoogleCalendarLifecycleCommand(calendar_connection.id, calendar_connection.lifecycle_generation)


@transaction.atomic
def request_google_calendar_workspace_reconciliation(workspace_id):
    """Advance connected grants in stable order for a workspace policy change."""

    commands = []
    for calendar_connection in lock_google_calendar_workspace_connections(workspace_id):
        if calendar_connection.desired_state != GoogleCalendarConnection.DesiredState.CONNECTED:
            continue
        if not has_usable_google_calendar_grant(calendar_connection):
            continue
        if calendar_connection.status != GoogleCalendarConnection.Status.PENDING:
            _transition(
                calendar_connection,
                calendar_connection.lifecycle_generation,
                GoogleCalendarConnection.DesiredState.CONNECTED,
                GoogleCalendarConnection.Status.PENDING,
                advance_generation=True,
            )
        else:
            calendar_connection.lifecycle_generation += 1
            calendar_connection.save(update_fields=["lifecycle_generation", "updated_at"])
        calendar_connection.last_error = ""
        calendar_connection.save(update_fields=["last_error", "updated_at"])
        commands.append(
            GoogleCalendarLifecycleCommand(calendar_connection.id, calendar_connection.lifecycle_generation)
        )
    return commands


@transaction.atomic
def request_google_calendar_workspace_policy_enable(workspace_id):
    """Advance retained usable grants after all disabled calendars are gone."""

    calendar_connections = lock_google_calendar_workspace_connections(workspace_id)
    if any(
        calendar_connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        for calendar_connection in calendar_connections
    ):
        raise GoogleCalendarDisableCleanupInProgress("Google Calendar workspace disable cleanup is still in progress")

    commands = []
    for calendar_connection in calendar_connections:
        if calendar_connection.desired_state != GoogleCalendarConnection.DesiredState.DISCONNECTED:
            continue
        if not calendar_connection.retain_grant_after_cleanup:
            continue
        if not has_usable_google_calendar_grant(calendar_connection):
            continue
        _transition(
            calendar_connection,
            calendar_connection.lifecycle_generation,
            GoogleCalendarConnection.DesiredState.CONNECTED,
            GoogleCalendarConnection.Status.PENDING,
            advance_generation=True,
        )
        calendar_connection.retain_grant_after_cleanup = False
        calendar_connection.last_error = ""
        calendar_connection.save(update_fields=["retain_grant_after_cleanup", "last_error", "updated_at"])
        commands.append(
            GoogleCalendarLifecycleCommand(calendar_connection.id, calendar_connection.lifecycle_generation)
        )
    return commands


@transaction.atomic
def request_google_calendar_workspace_policy_disable(workspace_id):
    """Advance every present connection to an absent cleanup generation."""

    commands = []
    for calendar_connection in lock_google_calendar_workspace_connections(workspace_id):
        if calendar_connection.desired_state != GoogleCalendarConnection.DesiredState.CONNECTED:
            continue
        _transition(
            calendar_connection,
            calendar_connection.lifecycle_generation,
            GoogleCalendarConnection.DesiredState.DISCONNECTED,
            GoogleCalendarConnection.Status.CLEANUP_PENDING,
            advance_generation=True,
        )
        calendar_connection.retain_grant_after_cleanup = True
        clear_google_calendar_oauth_attempt(calendar_connection)
        calendar_connection.last_error = ""
        calendar_connection.save(
            update_fields=[
                "retain_grant_after_cleanup",
                *_OAUTH_ATTEMPT_FIELDS,
                "last_error",
                "updated_at",
            ]
        )
        commands.append(
            GoogleCalendarLifecycleCommand(calendar_connection.id, calendar_connection.lifecycle_generation)
        )
    return commands


@transaction.atomic
def request_google_calendar_disconnect(connection_id, expected_generation):
    """Make a member grant cleanup-pending without performing provider HTTP."""

    calendar_connection = lock_google_calendar_connection(connection_id)
    _assert_generation(calendar_connection, expected_generation)
    if (
        calendar_connection.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        and calendar_connection.status == GoogleCalendarConnection.Status.DISCONNECTED
        and not any(
            (
                calendar_connection.provider_account_id,
                calendar_connection.provider_email,
                calendar_connection.calendar_id,
                calendar_connection.calendar_operation_id,
                calendar_connection.access_token,
                calendar_connection.refresh_token,
                calendar_connection.token_expires_at,
                calendar_connection.scopes,
            )
        )
    ):
        clear_google_calendar_oauth_attempt(calendar_connection)
        calendar_connection.retain_grant_after_cleanup = False
        calendar_connection.last_error = ""
        calendar_connection.save(
            update_fields=[
                "retain_grant_after_cleanup",
                *_OAUTH_ATTEMPT_FIELDS,
                "last_error",
                "updated_at",
            ]
        )
        return None
    if calendar_connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING:
        calendar_connection.lifecycle_generation = expected_generation + 1
        calendar_connection.save(update_fields=["lifecycle_generation", "updated_at"])
    else:
        _transition(
            calendar_connection,
            expected_generation,
            GoogleCalendarConnection.DesiredState.DISCONNECTED,
            GoogleCalendarConnection.Status.CLEANUP_PENDING,
            advance_generation=True,
        )
    calendar_connection.retain_grant_after_cleanup = False
    clear_google_calendar_oauth_attempt(calendar_connection)
    calendar_connection.last_error = ""
    calendar_connection.save(
        update_fields=[
            "retain_grant_after_cleanup",
            *_OAUTH_ATTEMPT_FIELDS,
            "last_error",
            "updated_at",
        ]
    )
    return GoogleCalendarLifecycleCommand(calendar_connection.id, calendar_connection.lifecycle_generation)


@transaction.atomic
def mark_google_calendar_connection_active(connection_id, expected_generation):
    """Finish reconciliation only when the worker still owns the generation."""

    calendar_connection = lock_google_calendar_connection(connection_id)
    _transition(
        calendar_connection,
        expected_generation,
        GoogleCalendarConnection.DesiredState.CONNECTED,
        GoogleCalendarConnection.Status.ACTIVE,
    )
    calendar_connection.last_success_at = timezone.now()
    calendar_connection.last_error = ""
    calendar_connection.save(update_fields=["last_success_at", "last_error", "updated_at"])
    return calendar_connection


@transaction.atomic
def mark_google_calendar_connection_error(connection_id, expected_generation, error):
    """Record a nonterminal reconciliation error for the exact generation."""

    calendar_connection = lock_google_calendar_connection(connection_id)
    _transition(
        calendar_connection,
        expected_generation,
        GoogleCalendarConnection.DesiredState.CONNECTED,
        GoogleCalendarConnection.Status.ERROR,
    )
    calendar_connection.last_error = str(error)
    calendar_connection.save(update_fields=["last_error", "updated_at"])
    return calendar_connection


@transaction.atomic
def record_google_calendar_cleanup_error(connection_id, expected_generation, error):
    """Keep unfinished cleanup nonterminal while recording its latest error."""

    calendar_connection = lock_google_calendar_connection(connection_id)
    _assert_generation(calendar_connection, expected_generation)
    if (
        calendar_connection.desired_state != GoogleCalendarConnection.DesiredState.DISCONNECTED
        or calendar_connection.status != GoogleCalendarConnection.Status.CLEANUP_PENDING
    ):
        raise IllegalGoogleCalendarTransition("Cleanup errors require a cleanup-pending connection")
    calendar_connection.last_error = str(error)
    calendar_connection.save(update_fields=["last_error", "updated_at"])
    return calendar_connection


@transaction.atomic
def complete_google_calendar_disconnect(connection_id, expected_generation):
    """Finish provider cleanup according to the durable generation intent."""

    calendar_connection = lock_google_calendar_connection(connection_id)
    retain_grant = calendar_connection.retain_grant_after_cleanup
    _transition(
        calendar_connection,
        expected_generation,
        GoogleCalendarConnection.DesiredState.DISCONNECTED,
        GoogleCalendarConnection.Status.DISCONNECTED,
    )
    calendar_connection.calendar_id = ""
    if not retain_grant:
        _clear_provider_grant(calendar_connection)
    clear_google_calendar_oauth_attempt(calendar_connection)
    calendar_connection.last_error = ""
    calendar_connection.save(
        update_fields=["calendar_id", *_PROVIDER_FIELDS, *_OAUTH_ATTEMPT_FIELDS, "last_error", "updated_at"]
    )
    return calendar_connection
