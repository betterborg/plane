# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import Mock, call, patch
from uuid import UUID, uuid4

import pytest
from django.db import close_old_connections, transaction
from django.utils import timezone

from plane.bgtasks.google_calendar_task import (
    _complete_absent,
    _converge_provider_event,
    backfill_google_calendar_cycles,
    backfill_google_calendar_open_issues,
    reconcile_google_calendar_connection,
    reconcile_google_calendar_inventory,
    synchronize_google_calendar_issue,
)
from plane.db.models import GoogleCalendarConnection, GoogleCalendarEvent
from plane.integrations.google_calendar.client import (
    GoogleCalendarCalendarAbsent,
    GoogleCalendarClientError,
    GoogleCalendarEventPage,
)
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_CYCLE_SYNC_TASK,
    GOOGLE_CALENDAR_INVENTORY_TASK,
    GOOGLE_CALENDAR_ISSUE_SYNC_TASK,
)
from plane.integrations.google_calendar.lifecycle import (
    apply_google_calendar_oauth_success,
    lock_google_calendar_connection,
    request_google_calendar_disconnect,
    request_google_calendar_workspace_policy_disable,
    request_google_calendar_workspace_policy_enable,
)
from plane.integrations.google_calendar.oauth import GOOGLE_CALENDAR_SCOPES, GoogleCalendarOAuthCredentials
from plane.license.utils.google_calendar_credentials import google_calendar_credential_fingerprint
from plane.tests.factories import (
    CycleFactory,
    CycleIssueFactory,
    GoogleCalendarConnectionFactory,
    IssueAssigneeFactory,
    IssueFactory,
    StateFactory,
    WorkspaceIntegrationFactory,
)


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

    @pytest.mark.parametrize(
        "token_expires_in",
        [timedelta(hours=1), -timedelta(minutes=1)],
        ids=["unexpired-access-token", "expired-access-token"],
    )
    def test_readiness_mismatch_marks_connection_broken_before_provider_http(self, caplog, token_expires_in):
        credential_fingerprint = google_calendar_credential_fingerprint("original-id", "original-secret")
        connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            calendar_id="recorded-plane-calendar",
            access_token="private-access-token",
            token_expires_at=timezone.now() + token_expires_in,
            credential_fingerprint=credential_fingerprint,
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            lifecycle_generation=3,
        )

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=GoogleCalendarOAuthCredentials("changed-id", "changed-secret"),
            ),
            patch("plane.integrations.google_calendar.client.requests.post") as post,
            patch("plane.integrations.google_calendar.client.requests.request") as request,
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 3)

        connection.refresh_from_db()
        assert result == "error"
        assert connection.status == GoogleCalendarConnection.Status.ERROR
        assert connection.last_error == "oauth_credentials_changed"
        post.assert_not_called()
        request.assert_not_called()
        for private_value in (
            "original-id",
            "original-secret",
            "changed-id",
            "changed-secret",
            credential_fingerprint,
        ):
            assert private_value not in caplog.text

    def test_cleanup_mismatch_resumes_only_after_exact_credentials_are_restored(self):
        credential_fingerprint = google_calendar_credential_fingerprint("original-id", "original-secret")
        connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            calendar_id="recorded-plane-calendar",
            access_token="access-token",
            refresh_token="refresh-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
            credential_fingerprint=credential_fingerprint,
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=4,
        )

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=GoogleCalendarOAuthCredentials("changed-id", "changed-secret"),
            ),
            patch("plane.integrations.google_calendar.client.requests.post") as post,
            patch("plane.integrations.google_calendar.client.requests.request") as request,
        ):
            mismatch_result = reconcile_google_calendar_connection(str(connection.id), 4)

        connection.refresh_from_db()
        assert mismatch_result == "error"
        assert connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert connection.calendar_id == "recorded-plane-calendar"
        assert connection.last_error == "oauth_credentials_changed"
        post.assert_not_called()
        request.assert_not_called()

        delete_response = Mock(status_code=204)
        revoke_response = Mock(status_code=200)
        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=GoogleCalendarOAuthCredentials("original-id", "original-secret"),
            ),
            patch(
                "plane.integrations.google_calendar.client.requests.request",
                return_value=delete_response,
            ) as request,
            patch(
                "plane.integrations.google_calendar.client.requests.post",
                return_value=revoke_response,
            ) as post,
        ):
            restored_result = reconcile_google_calendar_connection(str(connection.id), 4)

        connection.refresh_from_db()
        assert restored_result == "disconnected"
        assert connection.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert connection.calendar_id == ""
        assert connection.credential_fingerprint == ""
        assert connection.last_error == ""
        request.assert_called_once()
        post.assert_called_once()

    def test_calendarless_cleanup_validates_credentials_before_completing(self):
        connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            refresh_token="refresh-token",
            credential_fingerprint=google_calendar_credential_fingerprint("original-id", "original-secret"),
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=4,
            retain_grant_after_cleanup=True,
        )

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=GoogleCalendarOAuthCredentials("changed-id", "changed-secret"),
            ),
            patch("plane.integrations.google_calendar.client.requests.post") as post,
            patch("plane.integrations.google_calendar.client.requests.request") as request,
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 4)

        connection.refresh_from_db()
        assert result == "error"
        assert connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert connection.refresh_token == "refresh-token"
        assert connection.last_error == "oauth_credentials_changed"
        post.assert_not_called()
        request.assert_not_called()

    @pytest.mark.parametrize("retain_grant", [True, False], ids=["retained-grant", "shared-account"])
    def test_cleanup_completion_validates_credentials_before_skipping_revocation(self, retain_grant):
        connection = GoogleCalendarConnectionFactory(
            provider_account_id="shared-google-account",
            refresh_token="refresh-token",
            credential_fingerprint=google_calendar_credential_fingerprint("original-id", "original-secret"),
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=4,
            retain_grant_after_cleanup=retain_grant,
        )
        if not retain_grant:
            GoogleCalendarConnectionFactory(
                provider_account_id="shared-google-account",
                refresh_token="other-refresh-token",
            )
        correlation = GoogleCalendarEvent.objects.create(
            connection=connection,
            entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
            entity_id=uuid4(),
            google_event_id="event-id",
            payload_hash="payload-hash",
            calendar_generation=1,
            last_synced_at=timezone.now(),
        )

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=GoogleCalendarOAuthCredentials("changed-id", "changed-secret"),
            ),
            patch("plane.integrations.google_calendar.client.requests.post") as post,
            patch("plane.integrations.google_calendar.client.requests.request") as request,
        ):
            result = _complete_absent(connection, 4)

        connection.refresh_from_db()
        assert result == "error"
        assert connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert connection.last_error == "oauth_credentials_changed"
        assert GoogleCalendarEvent.objects.filter(id=correlation.id).exists()
        post.assert_not_called()
        request.assert_not_called()

    def test_event_write_mismatch_marks_active_connection_broken_without_provider_http(self):
        connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            calendar_id="recorded-plane-calendar",
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(seconds=30),
            credential_fingerprint=google_calendar_credential_fingerprint("original-id", "original-secret"),
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
            lifecycle_generation=3,
        )

        with (
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=GoogleCalendarOAuthCredentials("changed-id", "changed-secret"),
            ),
            patch("plane.integrations.google_calendar.client.requests.post") as post,
            patch("plane.integrations.google_calendar.client.requests.request") as request,
        ):
            result = _converge_provider_event(
                connection,
                GoogleCalendarEvent.EntityType.WORK_ITEM,
                uuid4(),
                {"summary": "Work item"},
                None,
            )

        connection.refresh_from_db()
        assert result == "credential_mismatch"
        assert connection.status == GoogleCalendarConnection.Status.ERROR
        assert connection.last_error == "oauth_credentials_changed"
        assert not GoogleCalendarEvent.objects.filter(connection=connection).exists()
        post.assert_not_called()
        request.assert_not_called()

    def test_readiness_persists_a_single_refreshed_access_token(self):
        credentials = GoogleCalendarOAuthCredentials("client-id", "client-secret")
        connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            calendar_id="recorded-plane-calendar",
            access_token="expired-access-token",
            refresh_token="refresh-token",
            token_expires_at=timezone.now() - timedelta(minutes=1),
            credential_fingerprint=google_calendar_credential_fingerprint(
                credentials.client_id,
                credentials.client_secret,
            ),
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            lifecycle_generation=3,
        )
        refresh_response = Mock(status_code=200)
        refresh_response.json.return_value = {"access_token": "fresh-access-token", "expires_in": 3600}
        calendar_response = Mock(status_code=200)
        calendar_response.json.return_value = {"id": "recorded-plane-calendar"}

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=credentials,
            ),
            patch(
                "plane.integrations.google_calendar.client.requests.post",
                return_value=refresh_response,
            ) as post,
            patch(
                "plane.integrations.google_calendar.client.requests.request",
                return_value=calendar_response,
            ) as request,
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 3)

        connection.refresh_from_db()
        assert result == "active"
        assert connection.access_token == "fresh-access-token"
        assert connection.status == GoogleCalendarConnection.Status.ACTIVE
        post.assert_called_once()
        request.assert_called_once()

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
        assert connection.calendar_generation == 1
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
        assert connection.calendar_generation == 1

    def test_present_generation_keeps_generation_for_the_recorded_calendar(self):
        connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            refresh_token="refresh-token",
            calendar_id="recorded-plane-calendar",
            calendar_generation=4,
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            lifecycle_generation=3,
        )
        client = _provider_client()

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 3)

        connection.refresh_from_db()
        assert result == "active"
        assert connection.calendar_id == "recorded-plane-calendar"
        assert connection.calendar_generation == 4
        client.find_calendar.assert_not_called()
        client.create_calendar.assert_not_called()

    def test_accessible_reconnect_retains_inventory_and_requests_expected_state_scan(self):
        connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            refresh_token="refresh-token",
            calendar_id="recorded-plane-calendar",
            calendar_generation=4,
            sync_token="retained-sync-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            lifecycle_generation=3,
        )
        client = _provider_client()

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as enqueue,
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 3)

        connection.refresh_from_db()
        assert result == "active"
        assert connection.calendar_generation == 4
        assert connection.sync_token == "retained-sync-token"
        assert [call.args[0] for call in enqueue.call_args_list] == [
            backfill_google_calendar_open_issues,
            backfill_google_calendar_cycles,
            reconcile_google_calendar_inventory,
        ]
        assert enqueue.call_args_list[-1].kwargs == {"force_local_scan": True}

    def test_accessible_reconnect_deletes_an_obsolete_retained_event(self):
        connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            refresh_token="refresh-token",
            calendar_id="recorded-plane-calendar",
            calendar_generation=4,
            sync_token="retained-sync-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            lifecycle_generation=3,
        )
        correlation = GoogleCalendarEvent.objects.create(
            connection=connection,
            entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
            entity_id=uuid4(),
            google_event_id="obsolete-provider-event",
            payload_hash="0" * 64,
            calendar_generation=4,
            provider_payload_hash="0" * 64,
            provider_status="confirmed",
        )
        client = _provider_client()
        client.list_event_page.return_value = GoogleCalendarEventPage((), None, "next-sync-token")

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit"),
        ):
            assert reconcile_google_calendar_connection(str(connection.id), 3) == "active"

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
        ):
            assert reconcile_google_calendar_inventory.run(str(connection.id), force_local_scan=True) == "continued"
            continuation = publish.call_args
            assert continuation.args[0].task == GOOGLE_CALENDAR_INVENTORY_TASK
            publish.reset_mock()

            def execute_sync(task, entity_id, connection_id):
                assert task.task == GOOGLE_CALENDAR_ISSUE_SYNC_TASK
                synchronize_google_calendar_issue.run(entity_id, connection_id)

            publish.side_effect = execute_sync
            assert reconcile_google_calendar_inventory.run(*continuation.args[1:]) == "complete"

        assert not GoogleCalendarEvent.objects.filter(id=correlation.id).exists()
        client.delete_event.assert_called_once_with(connection.calendar_id, "obsolete-provider-event")

    def test_confirmed_missing_calendar_replaces_it_and_invalidates_old_generation_observations(self):
        connection = GoogleCalendarConnectionFactory(
            provider_account_id="google-account",
            refresh_token="refresh-token",
            calendar_id="missing-plane-calendar",
            calendar_generation=4,
            sync_token="old-sync-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            lifecycle_generation=3,
        )
        correlation = GoogleCalendarEvent.objects.create(
            connection=connection,
            entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
            entity_id=uuid4(),
            google_event_id="old-provider-event",
            payload_hash="0" * 64,
            calendar_generation=4,
            provider_etag='"old"',
            provider_payload_hash="0" * 64,
            provider_status="confirmed",
        )
        client = _provider_client()
        client.get_calendar.side_effect = GoogleCalendarCalendarAbsent("confirmed absent")

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 3)

        connection.refresh_from_db()
        correlation.refresh_from_db()
        assert result == "active"
        assert connection.calendar_id == "new-plane-calendar"
        assert connection.calendar_generation == 5
        assert connection.sync_token == ""
        assert correlation.calendar_generation == 4
        assert correlation.provider_etag == ""
        assert correlation.provider_payload_hash == ""
        assert correlation.provider_status == ""
        client.find_calendar.assert_called_once()
        client.create_calendar.assert_called_once()

    def test_missing_calendar_replacement_backfills_only_current_open_entities(self):
        workspace_integration = WorkspaceIntegrationFactory(
            integration__provider="google_calendar",
            config={"enabled": True, "mode": "assignment", "recipients": "cycle_members"},
        )
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="google-account",
            refresh_token="refresh-token",
            calendar_id="missing-plane-calendar",
            calendar_generation=2,
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            lifecycle_generation=4,
        )
        open_issue = IssueFactory(project__workspace=workspace_integration.workspace)
        completed_issue = IssueFactory(
            project=open_issue.project,
            state=StateFactory(project=open_issue.project, group="completed"),
        )
        cycle_issue = IssueFactory(project=open_issue.project)
        for issue in (open_issue, completed_issue, cycle_issue):
            IssueAssigneeFactory(issue=issue, assignee=connection.member, project=issue.project)
        active_cycle = CycleFactory(project=open_issue.project)
        ended_cycle = CycleFactory(
            project=open_issue.project,
            start_date=timezone.now() - timedelta(days=10),
            end_date=timezone.now() - timedelta(days=2),
        )
        CycleIssueFactory(cycle=active_cycle, issue=cycle_issue, project=open_issue.project)
        CycleIssueFactory(cycle=ended_cycle, issue=cycle_issue, project=open_issue.project)
        client = _provider_client()
        client.get_calendar.side_effect = GoogleCalendarCalendarAbsent("confirmed absent")

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit"),
        ):
            assert reconcile_google_calendar_connection(str(connection.id), 4) == "active"

        with patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as enqueue:
            backfill_google_calendar_open_issues.run(str(connection.id))
            backfill_google_calendar_cycles.run(str(connection.id))

        issue_ids = {
            call.args[1] for call in enqueue.call_args_list if call.args[0].task == GOOGLE_CALENDAR_ISSUE_SYNC_TASK
        }
        cycle_ids = {
            call.args[1] for call in enqueue.call_args_list if call.args[0].task == GOOGLE_CALENDAR_CYCLE_SYNC_TASK
        }
        assert issue_ids == {str(open_issue.id), str(cycle_issue.id)}
        assert str(completed_issue.id) not in issue_ids
        assert cycle_ids == {str(active_cycle.id)}
        assert str(ended_cycle.id) not in cycle_ids

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
        assert connection.calendar_generation == 1

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
        assert client.method_calls == [
            call.validate_credentials(),
            call.delete_calendar("old-plane-calendar"),
            call.validate_credentials(),
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
                credential_fingerprint="a" * 64,
            )
            reconcile_google_calendar_connection(str(connection.id), oauth.generation)

        connection.refresh_from_db()
        delete_client.delete_calendar.assert_called_once_with("old-plane-calendar")
        revoke_client.revoke_grant.assert_called_once_with()
        create_client.create_calendar.assert_called_once()
        assert connection.provider_account_id == "new-google-account"
        assert connection.calendar_id == "new-plane-calendar"
        assert connection.status == GoogleCalendarConnection.Status.ACTIVE
