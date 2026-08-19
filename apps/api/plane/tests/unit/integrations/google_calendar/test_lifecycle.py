# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from unittest.mock import patch
from uuid import UUID

import pytest
from django.db import transaction
from django.utils import timezone

from plane.db.models import GoogleCalendarConnection
from plane.integrations.google_calendar.lifecycle import (
    IllegalGoogleCalendarTransition,
    StaleGoogleCalendarGeneration,
    UnusableGoogleCalendarGrant,
    _stable_advisory_lock_key,
    apply_google_calendar_oauth_success,
    complete_google_calendar_disconnect,
    has_usable_google_calendar_grant,
    lock_google_calendar_connection,
    lock_google_calendar_workspace_connections,
    mark_google_calendar_connection_active,
    record_google_calendar_cleanup_error,
    request_google_calendar_disconnect,
    request_google_calendar_workspace_reconciliation,
)
from plane.tests.factories import GoogleCalendarConnectionFactory, WorkspaceIntegrationFactory


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarLifecycleLocks:
    def test_connection_lock_is_stable_and_global_first(self):
        calendar_connection = GoogleCalendarConnectionFactory()

        with patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock") as acquire_lock:
            with transaction.atomic():
                locked_connection = lock_google_calendar_connection(calendar_connection.id)

        assert locked_connection.id == calendar_connection.id
        assert acquire_lock.call_count == 2
        global_key = acquire_lock.call_args_list[0].args[0]
        connection_key = acquire_lock.call_args_list[1].args[0]
        assert global_key == _stable_advisory_lock_key("google-calendar:lifecycle:global")
        assert connection_key == _stable_advisory_lock_key(
            "google-calendar:lifecycle:connection", calendar_connection.id
        )
        assert connection_key == _stable_advisory_lock_key(
            "google-calendar:lifecycle:connection", str(calendar_connection.id)
        )

    def test_workspace_connections_lock_in_stable_order(self):
        workspace_integration = WorkspaceIntegrationFactory()
        later_connection = GoogleCalendarConnectionFactory(
            id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            workspace_integration=workspace_integration,
        )
        earlier_connection = GoogleCalendarConnectionFactory(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            workspace_integration=workspace_integration,
        )

        with patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock") as acquire_lock:
            with transaction.atomic():
                locked_connections = lock_google_calendar_workspace_connections(workspace_integration.workspace_id)

        assert [item.id for item in locked_connections] == [
            earlier_connection.id,
            later_connection.id,
        ]
        assert [call.args[0] for call in acquire_lock.call_args_list] == [
            _stable_advisory_lock_key("google-calendar:lifecycle:global"),
            _stable_advisory_lock_key("google-calendar:lifecycle:connection", earlier_connection.id),
            _stable_advisory_lock_key("google-calendar:lifecycle:connection", later_connection.id),
        ]


@pytest.mark.unit
@pytest.mark.django_db
class TestUsableGoogleCalendarGrant:
    def test_attempt_only_row_is_not_a_grant(self):
        attempt = GoogleCalendarConnectionFactory(attempt_only=True)

        assert has_usable_google_calendar_grant(attempt) is False

    def test_bound_tokenless_row_is_not_a_grant(self):
        tokenless = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ERROR,
        )

        assert has_usable_google_calendar_grant(tokenless) is False

    def test_expired_access_token_is_not_a_grant(self):
        checked_at = timezone.now()
        expired = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            access_token="expired-access-token",
            token_expires_at=checked_at - timedelta(seconds=1),
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
        )

        assert has_usable_google_calendar_grant(expired, at=checked_at) is False

    @pytest.mark.parametrize("token_kind", ["refresh", "access"])
    def test_bound_refresh_or_unexpired_access_token_is_a_grant(self, token_kind):
        checked_at = timezone.now()
        token_fields = (
            {"refresh_token": "validated-refresh-token"}
            if token_kind == "refresh"
            else {
                "access_token": "access-token",
                "token_expires_at": checked_at + timedelta(minutes=5),
            }
        )
        calendar_connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            **token_fields,
        )

        assert has_usable_google_calendar_grant(calendar_connection, at=checked_at) is True


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarLifecycleTransitions:
    def test_oauth_success_advances_generation_and_rejects_stale_consumer(self):
        attempt = GoogleCalendarConnectionFactory(attempt_only=True)

        with patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"):
            command = apply_google_calendar_oauth_success(
                attempt.id,
                0,
                provider_account_id="google-account",
                provider_email="member@example.com",
                refresh_token="validated-refresh-token",
                scopes=["calendar.events"],
            )

            with pytest.raises(StaleGoogleCalendarGeneration):
                mark_google_calendar_connection_active(attempt.id, 0)

            active = mark_google_calendar_connection_active(attempt.id, command.generation)

        assert command.connection_id == attempt.id
        assert command.generation == 1
        assert active.desired_state == GoogleCalendarConnection.DesiredState.CONNECTED
        assert active.status == GoogleCalendarConnection.Status.ACTIVE
        assert active.lifecycle_generation == 1
        assert active.oauth_state == ""

    def test_unusable_oauth_result_does_not_change_attempt_state(self):
        attempt = GoogleCalendarConnectionFactory(attempt_only=True)

        with patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"):
            with pytest.raises(UnusableGoogleCalendarGrant):
                apply_google_calendar_oauth_success(
                    attempt.id,
                    0,
                    provider_account_id="google-account",
                    provider_email="member@example.com",
                )

        attempt.refresh_from_db()
        assert attempt.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        assert attempt.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert attempt.lifecycle_generation == 0
        assert attempt.oauth_state

    def test_disconnect_cleanup_retains_tombstone_and_exact_generation(self):
        active = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            provider_email="member@example.com",
            access_token="access-token",
            refresh_token="refresh-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
            scopes=["calendar.events"],
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
            lifecycle_generation=3,
        )

        with patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"):
            command = request_google_calendar_disconnect(active.id, 3)
            pending = record_google_calendar_cleanup_error(active.id, command.generation, "provider unavailable")

            assert pending.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
            assert pending.provider_account_id == "google-account"
            assert pending.last_error == "provider unavailable"

            with pytest.raises(StaleGoogleCalendarGeneration):
                complete_google_calendar_disconnect(active.id, 3)

            tombstone = complete_google_calendar_disconnect(active.id, command.generation)

        assert command.generation == 4
        assert tombstone.id == active.id
        assert tombstone.lifecycle_generation == 4
        assert tombstone.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        assert tombstone.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert tombstone.provider_account_id == ""
        assert tombstone.provider_email == ""
        assert tombstone.access_token == ""
        assert tombstone.refresh_token == ""
        assert tombstone.token_expires_at is None
        assert tombstone.scopes == []
        assert tombstone.last_error == ""

    def test_terminal_cleanup_is_illegal_before_disconnect(self):
        active = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            refresh_token="refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
            lifecycle_generation=2,
        )

        with patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"):
            with pytest.raises(IllegalGoogleCalendarTransition):
                complete_google_calendar_disconnect(active.id, 2)

    def test_policy_reconciliation_retains_grants_and_returns_stable_commands(self):
        workspace_integration = WorkspaceIntegrationFactory()
        later_connection = GoogleCalendarConnectionFactory(
            id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            workspace_integration=workspace_integration,
            provider_account_id="later-account",
            refresh_token="later-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
            lifecycle_generation=5,
        )
        earlier_connection = GoogleCalendarConnectionFactory(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            workspace_integration=workspace_integration,
            provider_account_id="earlier-account",
            refresh_token="earlier-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ERROR,
            lifecycle_generation=8,
        )

        with patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"):
            commands = request_google_calendar_workspace_reconciliation(workspace_integration.workspace_id)

        earlier_connection.refresh_from_db()
        later_connection.refresh_from_db()
        assert [(command.connection_id, command.generation) for command in commands] == [
            (earlier_connection.id, 9),
            (later_connection.id, 6),
        ]
        for calendar_connection in (earlier_connection, later_connection):
            assert calendar_connection.desired_state == GoogleCalendarConnection.DesiredState.CONNECTED
            assert calendar_connection.status == GoogleCalendarConnection.Status.PENDING
            assert calendar_connection.refresh_token
