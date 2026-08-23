# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, call, patch
from uuid import UUID

import pytest
from django.db import close_old_connections, transaction
from django.utils import timezone

from plane.bgtasks.google_calendar_task import reconcile_google_calendar_connection
from plane.db.models import GoogleCalendarConnection
from plane.integrations.google_calendar.client import GoogleCalendarClientError
from plane.integrations.google_calendar.lifecycle import (
    apply_google_calendar_oauth_success,
    lock_google_calendar_connection,
    request_google_calendar_disconnect,
    request_google_calendar_workspace_policy_disable,
    request_google_calendar_workspace_policy_enable,
)
from plane.integrations.google_calendar.oauth import GOOGLE_CALENDAR_SCOPES
from plane.tests.factories import GoogleCalendarConnectionFactory, WorkspaceIntegrationFactory


def _provider_client():
    client = Mock()
    client.access_token = None
    client.find_calendar.return_value = None
    client.create_calendar.return_value = "new-plane-calendar"
    return client


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarConvergenceTask:
    def test_stale_generation_performs_no_provider_side_effect(self):
        connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            refresh_token="refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            lifecycle_generation=3,
        )

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient") as client_class,
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 2)

        assert result == "stale"
        client_class.assert_not_called()

    def test_present_generation_creates_and_records_exactly_one_calendar(self):
        connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            refresh_token="refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            lifecycle_generation=3,
        )
        client = _provider_client()

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
        ):
            first_result = reconcile_google_calendar_connection(str(connection.id), 3)
            delayed_result = reconcile_google_calendar_connection(str(connection.id), 3)

        connection.refresh_from_db()
        assert first_result == "active"
        assert delayed_result == "stale"
        assert connection.calendar_id == "new-plane-calendar"
        assert connection.status == GoogleCalendarConnection.Status.ACTIVE
        client.find_calendar.assert_called_once()
        client.create_calendar.assert_called_once_with(client.find_calendar.call_args.args[0])

    def test_present_generation_recovers_creation_after_result_commit_failure(self):
        connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            refresh_token="refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            lifecycle_generation=3,
        )
        first_client = _provider_client()
        recovered_client = _provider_client()
        recovered_client.find_calendar.return_value = "new-plane-calendar"

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=first_client),
            patch(
                "plane.bgtasks.google_calendar_task.mark_google_calendar_connection_active",
                side_effect=RuntimeError("result commit failed"),
            ),
            pytest.raises(RuntimeError, match="result commit failed"),
        ):
            reconcile_google_calendar_connection(str(connection.id), 3)

        connection.refresh_from_db()
        operation_id = connection.calendar_operation_id
        assert operation_id is not None
        assert connection.calendar_id == ""
        assert connection.status == GoogleCalendarConnection.Status.PENDING

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=recovered_client),
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 3)

        connection.refresh_from_db()
        assert result == "active"
        first_client.find_calendar.assert_called_once_with(operation_id)
        first_client.create_calendar.assert_called_once_with(operation_id)
        recovered_client.find_calendar.assert_called_once_with(operation_id)
        recovered_client.create_calendar.assert_not_called()
        assert connection.calendar_id == "new-plane-calendar"
        assert connection.calendar_operation_id is None

    def test_disable_recovers_and_deletes_an_unrecorded_created_calendar(self):
        workspace_integration = WorkspaceIntegrationFactory(config={"enabled": False})
        operation_id = UUID("12345678-1234-5678-1234-567812345678")
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="google-account",
            refresh_token="refresh-token",
            calendar_operation_id=operation_id,
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=4,
            retain_grant_after_cleanup=True,
        )
        client = _provider_client()
        client.find_calendar.return_value = "created-before-crash"

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 4)

        connection.refresh_from_db()
        assert result == "disabled"
        client.find_calendar.assert_called_once_with(operation_id)
        client.delete_calendar.assert_called_once_with("created-before-crash")
        assert connection.calendar_id == ""
        assert connection.calendar_operation_id is None

    def test_disable_deletes_calendar_but_retains_grant_for_reenable(self):
        workspace_integration = WorkspaceIntegrationFactory(config={"enabled": False})
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="google-account",
            provider_email="member@example.com",
            calendar_id="old-plane-calendar",
            refresh_token="refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=4,
            retain_grant_after_cleanup=True,
        )
        delete_client = _provider_client()
        create_client = _provider_client()

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.bgtasks.google_calendar_task.GoogleCalendarClient",
                side_effect=[delete_client, create_client],
            ),
        ):
            workspace_integration.config = {"enabled": True}
            workspace_integration.save(update_fields=["config", "updated_at"])
            assert reconcile_google_calendar_connection(str(connection.id), 4) == "disabled"
            command = request_google_calendar_workspace_policy_enable(workspace_integration.workspace_id)[0]
            assert reconcile_google_calendar_connection(str(connection.id), command.generation) == "active"

        connection.refresh_from_db()
        delete_client.delete_calendar.assert_called_once_with("old-plane-calendar")
        delete_client.revoke_grant.assert_not_called()
        create_client.create_calendar.assert_called_once()
        assert connection.provider_account_id == "google-account"
        assert connection.refresh_token == "refresh-token"
        assert connection.calendar_id == "new-plane-calendar"

    def test_terminal_disconnect_deletes_old_calendar_then_retains_tombstone(self):
        workspace_integration = WorkspaceIntegrationFactory(config={"enabled": True})
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="old-google-account",
            provider_email="old@example.com",
            calendar_id="old-plane-calendar",
            access_token="access-token",
            refresh_token="refresh-token",
            token_expires_at=timezone.now(),
            oauth_state="outstanding-attempt",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
            lifecycle_generation=6,
        )
        client = _provider_client()

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
        ):
            command = request_google_calendar_disconnect(connection.id, 6)
            workspace_integration.config = {"enabled": False}
            workspace_integration.save(update_fields=["config", "updated_at"])
            assert request_google_calendar_workspace_policy_disable(workspace_integration.workspace_id) == []
            result = reconcile_google_calendar_connection(str(connection.id), command.generation)

        connection.refresh_from_db()
        assert result == "disconnected"
        assert client.method_calls[:2] == [
            call.delete_calendar("old-plane-calendar"),
            call.revoke_grant(),
        ]
        assert connection.calendar_id == ""
        assert connection.provider_account_id == ""
        assert connection.access_token == ""
        assert connection.refresh_token == ""
        assert connection.oauth_state == ""
        assert connection.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert connection.retain_grant_after_cleanup is False

    def test_terminal_cleanup_commits_delete_before_attempting_revocation(self):
        workspace_integration = WorkspaceIntegrationFactory(config={"enabled": True})
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="old-google-account",
            calendar_id="old-plane-calendar",
            refresh_token="refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=7,
        )
        delete_client = _provider_client()

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=delete_client),
            patch(
                "plane.bgtasks.google_calendar_task._complete_absent",
                side_effect=RuntimeError("worker stopped before revoke"),
            ),
            pytest.raises(RuntimeError, match="worker stopped before revoke"),
        ):
            reconcile_google_calendar_connection(str(connection.id), 7)

        connection.refresh_from_db()
        assert connection.calendar_id == ""
        assert connection.refresh_token == "refresh-token"
        assert connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        delete_client.delete_calendar.assert_called_once_with("old-plane-calendar")
        delete_client.revoke_grant.assert_not_called()

        revoke_client = _provider_client()
        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=revoke_client),
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 7)

        connection.refresh_from_db()
        assert result == "disconnected"
        revoke_client.delete_calendar.assert_not_called()
        revoke_client.revoke_grant.assert_called_once_with()
        assert connection.refresh_token == ""
        assert connection.status == GoogleCalendarConnection.Status.DISCONNECTED

    def test_terminal_cleanup_retries_idempotent_revoke_after_result_commit_failure(self):
        workspace_integration = WorkspaceIntegrationFactory(config={"enabled": True})
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="old-google-account",
            refresh_token="refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=7,
        )
        first_client = _provider_client()

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=first_client),
            patch(
                "plane.bgtasks.google_calendar_task.complete_google_calendar_disconnect",
                side_effect=RuntimeError("result commit failed"),
            ),
            pytest.raises(RuntimeError, match="result commit failed"),
        ):
            reconcile_google_calendar_connection(str(connection.id), 7)

        connection.refresh_from_db()
        assert connection.refresh_token == "refresh-token"
        assert connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        first_client.revoke_grant.assert_called_once_with()

        retry_client = _provider_client()
        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=retry_client),
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 7)

        connection.refresh_from_db()
        assert result == "disconnected"
        retry_client.revoke_grant.assert_called_once_with()
        assert connection.refresh_token == ""

    def test_revocation_failure_keeps_terminal_cleanup_and_credentials_nonterminal(self):
        workspace_integration = WorkspaceIntegrationFactory(config={"enabled": True})
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="old-google-account",
            refresh_token="refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=7,
            retain_grant_after_cleanup=False,
        )
        client = _provider_client()
        client.revoke_grant.side_effect = GoogleCalendarClientError("Google Calendar grant revocation failed")

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 7)

        connection.refresh_from_db()
        assert result == "error"
        assert connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert connection.provider_account_id == "old-google-account"
        assert connection.refresh_token == "refresh-token"
        assert connection.last_error == "Google Calendar grant revocation failed"

    def test_calendar_delete_failure_keeps_old_account_cleanup_nonterminal(self):
        workspace_integration = WorkspaceIntegrationFactory(config={"enabled": True})
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="old-google-account",
            calendar_id="old-plane-calendar",
            refresh_token="refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=7,
        )
        client = _provider_client()
        client.delete_calendar.side_effect = GoogleCalendarClientError("Google Calendar deletion failed")

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 7)

        connection.refresh_from_db()
        assert result == "error"
        assert connection.calendar_id == "old-plane-calendar"
        assert connection.provider_account_id == "old-google-account"
        assert connection.refresh_token == "refresh-token"
        assert connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert connection.last_error == "Google Calendar deletion failed"
        client.revoke_grant.assert_not_called()

    def test_shared_account_is_revoked_only_by_the_last_token_bearing_connection(self):
        first_integration = WorkspaceIntegrationFactory(config={"enabled": True})
        second_integration = WorkspaceIntegrationFactory(config={"enabled": True})
        first = GoogleCalendarConnectionFactory(
            workspace_integration=first_integration,
            provider_account_id="shared-google-account",
            refresh_token="first-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=2,
        )
        second = GoogleCalendarConnectionFactory(
            workspace_integration=second_integration,
            provider_account_id="shared-google-account",
            refresh_token="second-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=5,
        )
        second_client = _provider_client()

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=second_client),
        ):
            reconcile_google_calendar_connection(str(first.id), 2)
            reconcile_google_calendar_connection(str(second.id), 5)

        second_client.revoke_grant.assert_called_once_with()

    def test_same_account_callback_commits_before_final_revoke_accounting(self):
        disconnect_integration = WorkspaceIntegrationFactory(config={"enabled": True})
        callback_integration = WorkspaceIntegrationFactory(config={"enabled": True})
        disconnecting = GoogleCalendarConnectionFactory(
            workspace_integration=disconnect_integration,
            provider_account_id="shared-google-account",
            refresh_token="old-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=4,
        )
        callback_connection = GoogleCalendarConnectionFactory(
            workspace_integration=callback_integration,
        )
        callback_locked = Event()
        callback_can_commit = Event()
        worker_started = Event()
        client = _provider_client()

        def commit_same_account_callback():
            close_old_connections()
            try:
                with transaction.atomic():
                    locked = lock_google_calendar_connection(callback_connection.id)
                    locked.provider_account_id = "shared-google-account"
                    locked.provider_email = "member@example.com"
                    locked.refresh_token = "new-refresh-token"
                    locked.desired_state = GoogleCalendarConnection.DesiredState.CONNECTED
                    locked.status = GoogleCalendarConnection.Status.PENDING
                    locked.lifecycle_generation = 1
                    locked.save(
                        update_fields=[
                            "provider_account_id",
                            "provider_email",
                            "refresh_token",
                            "desired_state",
                            "status",
                            "lifecycle_generation",
                            "updated_at",
                        ]
                    )
                    callback_locked.set()
                    assert callback_can_commit.wait(timeout=5)
            finally:
                close_old_connections()

        def run_final_cleanup():
            close_old_connections()
            try:
                assert callback_locked.wait(timeout=5)
                worker_started.set()
                return reconcile_google_calendar_connection(str(disconnecting.id), 4)
            finally:
                close_old_connections()

        with patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client):
            with ThreadPoolExecutor(max_workers=2) as executor:
                callback_future = executor.submit(commit_same_account_callback)
                worker_future = executor.submit(run_final_cleanup)
                assert worker_started.wait(timeout=5)
                assert worker_future.done() is False
                callback_can_commit.set()
                callback_future.result(timeout=5)
                assert worker_future.result(timeout=5) == "disconnected"

        client.revoke_grant.assert_not_called()

    def test_complete_account_switch_provisions_only_after_old_tombstone(self):
        workspace_integration = WorkspaceIntegrationFactory(config={"enabled": True})
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="old-google-account",
            provider_email="old@example.com",
            calendar_id="old-plane-calendar",
            refresh_token="old-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
            lifecycle_generation=8,
        )
        delete_client = _provider_client()
        revoke_client = _provider_client()
        create_client = _provider_client()

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.bgtasks.google_calendar_task.GoogleCalendarClient",
                side_effect=[delete_client, revoke_client, create_client],
            ),
        ):
            disconnect = request_google_calendar_disconnect(connection.id, 8)
            reconcile_google_calendar_connection(str(connection.id), disconnect.generation)
            connection.refresh_from_db()
            assert connection.provider_account_id == ""
            oauth = apply_google_calendar_oauth_success(
                connection.id,
                connection.lifecycle_generation,
                provider_account_id="new-google-account",
                provider_email="new@example.com",
                refresh_token="new-refresh-token",
                scopes=GOOGLE_CALENDAR_SCOPES,
            )
            reconcile_google_calendar_connection(str(connection.id), oauth.generation)

        connection.refresh_from_db()
        delete_client.delete_calendar.assert_called_once_with("old-plane-calendar")
        revoke_client.revoke_grant.assert_called_once_with()
        create_client.create_calendar.assert_called_once()
        assert connection.provider_account_id == "new-google-account"
        assert connection.calendar_id == "new-plane-calendar"
        assert connection.status == GoogleCalendarConnection.Status.ACTIVE
