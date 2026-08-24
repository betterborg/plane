# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch
from uuid import UUID

import pytest
from django.db import close_old_connections, transaction
from django.utils import timezone

from plane.db.models import GoogleCalendarConnection
from plane.integrations.google_calendar.lifecycle import (
    IllegalGoogleCalendarTransition,
    GoogleCalendarDisableCleanupInProgress,
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
    request_google_calendar_workspace_policy_disable,
    request_google_calendar_workspace_policy_enable,
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

    def test_retained_disconnected_refresh_token_is_a_usable_grant(self):
        calendar_connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            refresh_token="validated-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.DISCONNECTED,
        )

        assert has_usable_google_calendar_grant(calendar_connection) is True


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
                credential_fingerprint="a" * 64,
            )

            with pytest.raises(StaleGoogleCalendarGeneration):
                mark_google_calendar_connection_active(attempt.id, 0)

            active = mark_google_calendar_connection_active(attempt.id, command.generation)

        assert command.connection_id == attempt.id
        assert command.generation == 1
        assert active.desired_state == GoogleCalendarConnection.DesiredState.CONNECTED
        assert active.status == GoogleCalendarConnection.Status.ACTIVE
        assert active.lifecycle_generation == 1
        assert active.last_success_at is not None
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
                    credential_fingerprint="a" * 64,
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
            sync_token="provider-sync-token",
            page_token="provider-page-token",
            credential_fingerprint="a" * 64,
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
            assert pending.retain_grant_after_cleanup is False
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
        assert tombstone.sync_token == ""
        assert tombstone.page_token == ""
        assert tombstone.token_expires_at is None
        assert tombstone.scopes == []
        assert tombstone.credential_fingerprint == ""
        assert tombstone.last_error == ""

    @pytest.mark.parametrize(
        ("provider_field", "provider_value"),
        [
            ("sync_token", "provider-sync-token"),
            ("page_token", "provider-page-token"),
            ("credential_fingerprint", "a" * 64),
        ],
    )
    def test_terminal_row_with_reconciliation_provider_state_reopens_cleanup(
        self,
        provider_field,
        provider_value,
    ):
        calendar_connection = GoogleCalendarConnectionFactory(**{provider_field: provider_value})

        with patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"):
            command = request_google_calendar_disconnect(calendar_connection.id, 0)
            tombstone = complete_google_calendar_disconnect(calendar_connection.id, command.generation)

        assert command.generation == 1
        assert tombstone.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        assert tombstone.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert getattr(tombstone, provider_field) == ""

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

    def test_terminal_disconnect_supersedes_retained_policy_cleanup(self):
        pending_policy_cleanup = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            refresh_token="refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=4,
            retain_grant_after_cleanup=True,
        )

        with patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"):
            command = request_google_calendar_disconnect(pending_policy_cleanup.id, 4)

            with pytest.raises(StaleGoogleCalendarGeneration):
                complete_google_calendar_disconnect(pending_policy_cleanup.id, 4)
            with pytest.raises(GoogleCalendarDisableCleanupInProgress):
                request_google_calendar_workspace_policy_enable(
                    pending_policy_cleanup.workspace_integration.workspace_id
                )

            tombstone = complete_google_calendar_disconnect(pending_policy_cleanup.id, command.generation)

        assert command.generation == 5
        assert tombstone.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert tombstone.retain_grant_after_cleanup is False
        assert tombstone.provider_account_id == ""
        assert tombstone.refresh_token == ""

    def test_terminal_disconnect_reopens_a_completed_retained_policy_cleanup(self):
        retained_grant = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            refresh_token="refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.DISCONNECTED,
            lifecycle_generation=4,
            retain_grant_after_cleanup=True,
        )

        with patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"):
            command = request_google_calendar_disconnect(retained_grant.id, 4)

        retained_grant.refresh_from_db()
        assert command.generation == 5
        assert retained_grant.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert retained_grant.retain_grant_after_cleanup is False

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

    def test_policy_disable_cleanup_retains_grant_and_enable_advances_it(self):
        workspace_integration = WorkspaceIntegrationFactory()
        active = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="google-account",
            provider_email="member@example.com",
            calendar_id="plane-calendar",
            refresh_token="validated-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
            lifecycle_generation=3,
        )
        unusable = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="tokenless-account",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ERROR,
            lifecycle_generation=7,
        )

        with patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"):
            disable_commands = request_google_calendar_workspace_policy_disable(workspace_integration.workspace_id)

            with pytest.raises(GoogleCalendarDisableCleanupInProgress):
                request_google_calendar_workspace_policy_enable(workspace_integration.workspace_id)

            active.refresh_from_db()
            assert active.retain_grant_after_cleanup is True
            complete_google_calendar_disconnect(active.id, active.lifecycle_generation)
            enable_commands = request_google_calendar_workspace_policy_enable(workspace_integration.workspace_id)

        active.refresh_from_db()
        unusable.refresh_from_db()
        assert {(command.connection_id, command.generation) for command in disable_commands} == {
            (active.id, 4),
            (unusable.id, 8),
        }
        assert [(command.connection_id, command.generation) for command in enable_commands] == [(active.id, 5)]
        assert active.desired_state == GoogleCalendarConnection.DesiredState.CONNECTED
        assert active.status == GoogleCalendarConnection.Status.PENDING
        assert active.calendar_id == ""
        assert active.provider_account_id == "google-account"
        assert active.refresh_token == "validated-refresh-token"
        assert unusable.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        assert unusable.lifecycle_generation == 8

    def test_policy_enable_is_blocked_by_an_unresolved_calendar_operation(self):
        workspace_integration = WorkspaceIntegrationFactory()
        calendar_connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="google-account",
            refresh_token="validated-refresh-token",
            calendar_operation_id=UUID("12345678-1234-5678-1234-567812345678"),
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=4,
            retain_grant_after_cleanup=True,
        )

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            pytest.raises(GoogleCalendarDisableCleanupInProgress),
        ):
            request_google_calendar_workspace_policy_enable(workspace_integration.workspace_id)

        calendar_connection.refresh_from_db()
        assert calendar_connection.lifecycle_generation == 4
        assert calendar_connection.calendar_operation_id is not None


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarWorkspacePolicyRaces:
    def test_reenable_waits_for_cleanup_then_advances_one_generation(self):
        workspace_integration = WorkspaceIntegrationFactory()
        calendar_connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="google-account",
            calendar_id="calendar-awaiting-delete",
            refresh_token="validated-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=4,
            retain_grant_after_cleanup=True,
        )
        cleanup_locked = Barrier(2)
        cleanup_can_finish = Event()
        enable_started = Event()

        def finish_cleanup():
            close_old_connections()
            try:
                with transaction.atomic():
                    locked_connection = lock_google_calendar_connection(calendar_connection.id)
                    cleanup_locked.wait(timeout=5)
                    assert cleanup_can_finish.wait(timeout=5)
                    return complete_google_calendar_disconnect(
                        locked_connection.id,
                        locked_connection.lifecycle_generation,
                    )
            finally:
                close_old_connections()

        def reenable_workspace():
            close_old_connections()
            try:
                cleanup_locked.wait(timeout=5)
                enable_started.set()
                return request_google_calendar_workspace_policy_enable(workspace_integration.workspace_id)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            cleanup_future = executor.submit(finish_cleanup)
            enable_future = executor.submit(reenable_workspace)
            assert enable_started.wait(timeout=5)
            assert enable_future.done() is False
            cleanup_can_finish.set()
            cleanup_future.result(timeout=5)
            commands = enable_future.result(timeout=5)

        calendar_connection.refresh_from_db()
        assert [(command.connection_id, command.generation) for command in commands] == [(calendar_connection.id, 5)]
        assert calendar_connection.calendar_id == ""
        assert calendar_connection.desired_state == GoogleCalendarConnection.DesiredState.CONNECTED
        assert calendar_connection.status == GoogleCalendarConnection.Status.PENDING
        assert calendar_connection.lifecycle_generation == 5

    def test_reenable_waits_for_unfinished_cleanup_then_conflicts_without_mutation(self):
        workspace_integration = WorkspaceIntegrationFactory()
        calendar_connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="google-account",
            calendar_id="calendar-awaiting-delete",
            refresh_token="validated-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=4,
        )
        cleanup_locked = Barrier(2)
        cleanup_can_finish = Event()
        enable_started = Event()

        def retain_unfinished_cleanup():
            close_old_connections()
            try:
                with transaction.atomic():
                    locked_connection = lock_google_calendar_connection(calendar_connection.id)
                    cleanup_locked.wait(timeout=5)
                    assert cleanup_can_finish.wait(timeout=5)
                    locked_connection.last_error = "provider unavailable"
                    locked_connection.save(update_fields=["last_error", "updated_at"])
            finally:
                close_old_connections()

        def reenable_workspace():
            close_old_connections()
            try:
                cleanup_locked.wait(timeout=5)
                enable_started.set()
                return request_google_calendar_workspace_policy_enable(workspace_integration.workspace_id)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            cleanup_future = executor.submit(retain_unfinished_cleanup)
            enable_future = executor.submit(reenable_workspace)
            assert enable_started.wait(timeout=5)
            assert enable_future.done() is False
            cleanup_can_finish.set()
            cleanup_future.result(timeout=5)
            with pytest.raises(GoogleCalendarDisableCleanupInProgress):
                enable_future.result(timeout=5)

        calendar_connection.refresh_from_db()
        assert calendar_connection.calendar_id == "calendar-awaiting-delete"
        assert calendar_connection.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        assert calendar_connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert calendar_connection.lifecycle_generation == 4
