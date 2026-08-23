# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, current_thread, main_thread
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from celery.exceptions import Retry
from django.db import close_old_connections
from django.utils import timezone

from plane.bgtasks.google_calendar_task import (
    _connection_ids_for_issue,
    _synchronize_issue_for_connection,
    backfill_google_calendar_open_issues,
    reconcile_google_calendar_connection,
    resync_google_calendar_workspace_issues,
    synchronize_google_calendar_issue,
)
from plane.db.models import GoogleCalendarEvent
from plane.integrations.google_calendar.client import GoogleCalendarClient, GoogleCalendarClientConflict
from plane.integrations.google_calendar.lifecycle import (
    request_google_calendar_workspace_policy_disable,
    request_google_calendar_workspace_policy_enable,
)
from plane.tests.factories import (
    GoogleCalendarConnectionFactory,
    IssueAssigneeFactory,
    IssueFactory,
    StateFactory,
    UserFactory,
    WorkspaceIntegrationFactory,
)


def _provider_client():
    client = Mock()
    client.access_token = None
    client.get_event.return_value = {"id": "existing-event"}
    client.list_events.return_value = []
    return client


@pytest.fixture(autouse=True)
def calendar_app_base_url(settings):
    settings.APP_BASE_URL = "https://plane.example"


@pytest.mark.unit
@pytest.mark.django_db
class TestGoogleCalendarWorkItemTask:
    def setup_method(self):
        self.workspace_integration = WorkspaceIntegrationFactory(
            config={"enabled": True, "mode": "assignment", "update_on_completion": True}
        )
        self.issue = IssueFactory(project__workspace=self.workspace_integration.workspace)
        self.connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            active=True,
        )
        IssueAssigneeFactory(issue=self.issue, assignee=self.connection.member, project=self.issue.project)

    def test_fans_out_only_to_healthy_connected_assignees_and_is_idempotent(self):
        second_connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            member=UserFactory(),
            active=True,
        )
        IssueAssigneeFactory(issue=self.issue, assignee=second_connection.member, project=self.issue.project)
        unavailable_connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            member=UserFactory(),
            status="error",
            desired_state="connected",
        )
        IssueAssigneeFactory(issue=self.issue, assignee=unavailable_connection.member, project=self.issue.project)
        client = _provider_client()

        with patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client):
            first_result = synchronize_google_calendar_issue.run(str(self.issue.id))
            second_result = synchronize_google_calendar_issue.run(str(self.issue.id))

        assert first_result.count("created") == 2
        assert "ineligible" in first_result
        assert second_result.count("unchanged") == 2
        assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 2
        assert client.insert_event.call_count == 2

    def test_insert_conflict_recovers_the_deterministic_event_without_a_duplicate(self):
        client = _provider_client()
        client.insert_event.side_effect = GoogleCalendarClientConflict("already exists")

        with patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client):
            result = synchronize_google_calendar_issue.run(str(self.issue.id))

        assert result == ["created"]
        assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 1
        client.get_event.assert_called_once()
        client.update_event.assert_called_once()

    def test_completion_updates_then_reopening_restores_the_normal_payload(self):
        client = _provider_client()
        with patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client):
            synchronize_google_calendar_issue.run(str(self.issue.id))
            open_state = self.issue.state
            self.issue.state = StateFactory(project=self.issue.project, group="completed", name="Done")
            self.issue.save(update_fields=["state", "completed_at"])
            completed_result = synchronize_google_calendar_issue.run(str(self.issue.id))
            self.issue.state = open_state
            self.issue.save(update_fields=["state", "completed_at"])
            reopened_result = synchronize_google_calendar_issue.run(str(self.issue.id))

        assert completed_result == ["updated"]
        assert reopened_result == ["updated"]
        completed_payload = client.update_event.call_args_list[-2].args[2]
        reopened_payload = client.update_event.call_args_list[-1].args[2]
        assert completed_payload["summary"].startswith("[Completed]")
        assert completed_payload["colorId"] == "10"
        assert reopened_payload["summary"].startswith(f"[{self.issue.project.identifier}-")
        assert "colorId" not in reopened_payload
        assert reopened_payload["status"] == "confirmed"

    def test_delete_completion_policy_removes_the_event(self):
        client = _provider_client()
        with patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client):
            synchronize_google_calendar_issue.run(str(self.issue.id))
            self.workspace_integration.config["update_on_completion"] = False
            self.workspace_integration.save(update_fields=["config", "updated_at"])
            self.issue.state = StateFactory(project=self.issue.project, group="cancelled", name="Cancelled")
            self.issue.save(update_fields=["state", "completed_at"])
            result = synchronize_google_calendar_issue.run(str(self.issue.id))

        assert result == ["deleted"]
        assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 0
        client.delete_event.assert_called_once()

    def test_delete_retry_converges_after_provider_success_and_database_rollback(self):
        client = _provider_client()
        with patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client):
            synchronize_google_calendar_issue.run(str(self.issue.id))

        self.workspace_integration.config["update_on_completion"] = False
        self.workspace_integration.save(update_fields=["config", "updated_at"])
        self.issue.state = StateFactory(project=self.issue.project, group="cancelled", name="Cancelled")
        self.issue.save(update_fields=["state", "completed_at"])

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch.object(GoogleCalendarEvent, "delete", side_effect=RuntimeError("database write failed")),
            pytest.raises(RuntimeError, match="database write failed"),
        ):
            synchronize_google_calendar_issue.run(str(self.issue.id))

        assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 1
        client.delete_event.assert_called_once()

        gone_response = Mock(status_code=410)
        retry_client = GoogleCalendarClient(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )
        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=retry_client),
            patch(
                "plane.integrations.google_calendar.client.requests.request",
                return_value=gone_response,
            ),
        ):
            result = synchronize_google_calendar_issue.run(str(self.issue.id))

        assert result == ["deleted"]
        assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 0
        gone_response.raise_for_status.assert_not_called()

    def test_assignment_loss_removes_the_affected_event(self):
        client = _provider_client()
        with patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client):
            synchronize_google_calendar_issue.run(str(self.issue.id))
            self.issue.issue_assignee.get(assignee=self.connection.member).delete()
            result = synchronize_google_calendar_issue.run(str(self.issue.id))

        assert result == ["deleted"]
        assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 0
        client.delete_event.assert_called_once()

    def test_backfill_publishes_open_and_overdue_but_not_terminal_items(self):
        overdue = self.issue
        completed = IssueFactory(
            project=self.issue.project,
            state=StateFactory(project=self.issue.project, group="completed"),
        )
        IssueAssigneeFactory(issue=completed, assignee=self.connection.member, project=self.issue.project)

        with patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as publish:
            published = backfill_google_calendar_open_issues.run(str(self.connection.id))

        assert published == 1
        assert publish.call_count == 1
        assert publish.call_args.args[1:] == (str(overdue.id), str(self.connection.id))

    def test_backfill_delays_the_next_page_until_the_current_page_pacing_window_ends(self):
        for _ in range(2):
            issue = IssueFactory(project=self.issue.project)
            IssueAssigneeFactory(issue=issue, assignee=self.connection.member, project=self.issue.project)

        with patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as publish:
            published = backfill_google_calendar_open_issues.run(str(self.connection.id), batch_size=2)

        assert published == 2
        assert publish.call_count == 3
        first_sync, second_sync, continuation = publish.call_args_list
        assert first_sync.args[0].task == "plane.bgtasks.google_calendar_task.synchronize_google_calendar_issue"
        assert first_sync.args[0].options["countdown"] == 0
        assert second_sync.args[0].task == "plane.bgtasks.google_calendar_task.synchronize_google_calendar_issue"
        assert second_sync.args[0].options["countdown"] == 1
        assert continuation.args[0].task == "plane.bgtasks.google_calendar_task.backfill_google_calendar_open_issues"
        assert continuation.args[0].options["countdown"] == 2

    def test_workspace_resync_includes_current_and_ledger_only_issue_ids_once(self):
        ledger_only_issue_id = uuid4()
        GoogleCalendarEvent.objects.create(
            connection=self.connection,
            entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
            entity_id=self.issue.id,
            google_event_id="current-issue-event",
            payload_hash="current-issue-payload",
        )
        GoogleCalendarEvent.objects.create(
            connection=self.connection,
            entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
            entity_id=ledger_only_issue_id,
            google_event_id="ledger-only-event",
            payload_hash="ledger-only-payload",
        )
        IssueFactory()

        with patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as publish:
            published = resync_google_calendar_workspace_issues.run(self.workspace_integration.workspace_id)

        assert published == 2
        assert {call.args[1] for call in publish.call_args_list} == {
            str(self.issue.id),
            str(ledger_only_issue_id),
        }
        assert all(
            call.args[0].task == "plane.bgtasks.google_calendar_task.synchronize_google_calendar_issue"
            for call in publish.call_args_list
        )

    def test_workspace_resync_paces_pages(self):
        second_issue = IssueFactory(project=self.issue.project)
        third_issue = IssueFactory(project=self.issue.project)

        with patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as publish:
            published = resync_google_calendar_workspace_issues.run(
                self.workspace_integration.workspace_id,
                batch_size=2,
            )

        assert published == 2
        assert publish.call_count == 3
        first_sync, second_sync, continuation = publish.call_args_list
        assert {first_sync.args[1], second_sync.args[1]} < {
            str(self.issue.id),
            str(second_issue.id),
            str(third_issue.id),
        }
        assert first_sync.args[0].options["countdown"] == 0
        assert second_sync.args[0].options["countdown"] == 1
        assert continuation.args[0].task == (
            "plane.bgtasks.google_calendar_task.resync_google_calendar_workspace_issues"
        )
        assert continuation.args[0].options["countdown"] == 2
        assert continuation.args[1] == str(self.workspace_integration.workspace_id)
        assert continuation.args[3] == 2

    def test_workspace_resync_waits_for_policy_lifecycle_reconciliation(self):
        self.connection.status = "pending"
        self.connection.save(update_fields=["status", "updated_at"])

        with (
            patch.object(resync_google_calendar_workspace_issues, "retry", side_effect=Retry()) as retry,
            patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as publish,
            pytest.raises(Retry),
        ):
            resync_google_calendar_workspace_issues.run(self.workspace_integration.workspace_id)

        retry.assert_called_once_with(countdown=5)
        publish.assert_not_called()

    def test_successful_initial_provisioning_enqueues_open_item_backfill(self):
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            provider_account_id="google-account",
            refresh_token="refresh-token",
            desired_state="connected",
            status="pending",
            lifecycle_generation=3,
        )
        client = _provider_client()
        client.find_calendar.return_value = None
        client.create_calendar.return_value = "new-calendar"

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as publish,
        ):
            result = reconcile_google_calendar_connection.run(str(connection.id), 3)

        assert result == "active"
        publish.assert_called_once()
        assert publish.call_args.args[1:] == (str(connection.id),)

    def test_cleanup_complete_reenable_enqueues_open_item_backfill(self):
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            provider_account_id="google-account",
            refresh_token="refresh-token",
            desired_state="disconnected",
            status="disconnected",
            retain_grant_after_cleanup=True,
            lifecycle_generation=4,
        )
        client = _provider_client()
        client.find_calendar.return_value = None
        client.create_calendar.return_value = "reenabled-calendar"

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as publish,
        ):
            command = request_google_calendar_workspace_policy_enable(self.workspace_integration.workspace_id)[0]
            result = reconcile_google_calendar_connection.run(str(connection.id), command.generation)

        assert result == "active"
        publish.assert_called_once()
        assert publish.call_args.args[1:] == (str(connection.id),)

    def test_workspace_disable_cleanup_removes_durable_event_correlations(self):
        client = _provider_client()
        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit"),
        ):
            synchronize_google_calendar_issue.run(str(self.issue.id))
            assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 1
            command = request_google_calendar_workspace_policy_disable(self.workspace_integration.workspace_id)[0]
            result = reconcile_google_calendar_connection.run(str(self.connection.id), command.generation)

        assert result == "disabled"
        assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 0
        client.delete_calendar.assert_called_once_with(self.connection.calendar_id)


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
def test_concurrent_delivery_creates_one_provider_event_and_one_ledger_row():
    workspace_integration = WorkspaceIntegrationFactory(
        config={"enabled": True, "mode": "assignment", "update_on_completion": True}
    )
    issue = IssueFactory(project__workspace=workspace_integration.workspace)
    connection = GoogleCalendarConnectionFactory(
        workspace_integration=workspace_integration,
        active=True,
    )
    IssueAssigneeFactory(issue=issue, assignee=connection.member, project=issue.project)
    client = _provider_client()

    def synchronize_in_thread():
        close_old_connections()
        try:
            return synchronize_google_calendar_issue.run(str(issue.id))
        finally:
            close_old_connections()

    with (
        patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        results = [future.result(timeout=10) for future in [executor.submit(synchronize_in_thread) for _ in range(2)]]

    assert sorted(results) == [["created"], ["unchanged"]]
    assert client.insert_event.call_count == 1
    assert GoogleCalendarEvent.objects.filter(connection=connection, entity_id=issue.id).count() == 1


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
def test_concurrent_delivery_of_hard_deleted_issue_removes_one_correlation_without_error():
    workspace_integration = WorkspaceIntegrationFactory(
        config={"enabled": True, "mode": "assignment", "update_on_completion": True}
    )
    issue = IssueFactory(project__workspace=workspace_integration.workspace)
    issue_id = issue.id
    connection = GoogleCalendarConnectionFactory(
        workspace_integration=workspace_integration,
        active=True,
    )
    IssueAssigneeFactory(issue=issue, assignee=connection.member, project=issue.project)
    client = _provider_client()

    with patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client):
        synchronize_google_calendar_issue.run(str(issue_id))
    issue.delete(soft=False)
    deliveries_ready = Barrier(2)

    def synchronize_after_both_deliveries_read_the_correlation(deleted_issue_id, connection_id):
        deliveries_ready.wait(timeout=10)
        return _synchronize_issue_for_connection(deleted_issue_id, connection_id)

    def synchronize_in_thread():
        close_old_connections()
        try:
            return synchronize_google_calendar_issue.run(str(issue_id))
        finally:
            close_old_connections()

    client.reset_mock()
    with (
        patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
        patch(
            "plane.bgtasks.google_calendar_task._synchronize_issue_for_connection",
            side_effect=synchronize_after_both_deliveries_read_the_correlation,
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        results = [future.result(timeout=10) for future in [executor.submit(synchronize_in_thread) for _ in range(2)]]

    assert results == ["missing", "missing"]
    assert client.delete_event.call_count == 1
    assert GoogleCalendarEvent.objects.filter(connection=connection, entity_id=issue_id).count() == 0


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
def test_older_delivery_cannot_overwrite_a_newer_issue_snapshot():
    workspace_integration = WorkspaceIntegrationFactory(
        config={"enabled": True, "mode": "assignment", "update_on_completion": True}
    )
    issue = IssueFactory(
        project__workspace=workspace_integration.workspace,
        name="Old title",
    )
    connection = GoogleCalendarConnectionFactory(
        workspace_integration=workspace_integration,
        active=True,
    )
    IssueAssigneeFactory(issue=issue, assignee=connection.member, project=issue.project)
    client = _provider_client()
    old_snapshot_loaded = Event()
    release_old_delivery = Event()

    def pause_old_delivery(stale_issue, connection_id=None):
        if current_thread() is not main_thread():
            assert stale_issue.name == "Old title"
            old_snapshot_loaded.set()
            assert release_old_delivery.wait(timeout=10)
        return _connection_ids_for_issue(stale_issue, connection_id)

    def synchronize_in_thread():
        close_old_connections()
        try:
            return synchronize_google_calendar_issue.run(str(issue.id))
        finally:
            close_old_connections()

    with (
        patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
        patch(
            "plane.bgtasks.google_calendar_task._connection_ids_for_issue",
            side_effect=pause_old_delivery,
        ),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        old_delivery = executor.submit(synchronize_in_thread)
        assert old_snapshot_loaded.wait(timeout=10)
        issue.name = "New title"
        issue.save(update_fields=["name", "updated_at"])
        newer_result = synchronize_google_calendar_issue.run(str(issue.id))
        release_old_delivery.set()
        older_result = old_delivery.result(timeout=10)

    assert newer_result == ["created"]
    assert older_result == ["unchanged"]
    assert client.insert_event.call_count == 1
    assert client.insert_event.call_args.args[2]["summary"].endswith("New title")
    assert GoogleCalendarEvent.objects.filter(connection=connection, entity_id=issue.id).count() == 1
