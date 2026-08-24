# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, current_thread, main_thread
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
import requests
from celery.exceptions import Retry
from django.db import close_old_connections
from django.utils import timezone

from plane.bgtasks.google_calendar_task import (
    _connection_ids_for_issue,
    _synchronize_issue_for_connection,
    backfill_google_calendar_cycles,
    backfill_google_calendar_open_issues,
    paced_google_calendar_issue_sync_tasks,
    reconcile_google_calendar_workspace_issue_resyncs,
    reconcile_google_calendar_connection,
    resync_google_calendar_label,
    resync_google_calendar_project_issues,
    resync_google_calendar_state_issues,
    resync_google_calendar_workspace_issues,
    synchronize_google_calendar_issue,
)
from plane.celery import app as celery_app
from plane.db.models import GoogleCalendarEvent, IssueLabel, Label
from plane.integrations.google_calendar.client import (
    GoogleCalendarClient,
    GoogleCalendarClientConflict,
    GoogleCalendarEventAbsent,
    GoogleCalendarProviderError,
)
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_ISSUE_SYNC_TASK,
    GOOGLE_CALENDAR_LABEL_ISSUE_RESYNC_TASK,
    GOOGLE_CALENDAR_LIFECYCLE_TASK,
    GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK,
    GOOGLE_CALENDAR_STATE_ISSUE_RESYNC_TASK,
    GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY,
    GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_RECONCILIATION_TASK,
    GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK,
)
from plane.integrations.google_calendar.lifecycle import (
    request_google_calendar_workspace_policy_disable,
    request_google_calendar_workspace_policy_enable,
)
from plane.integrations.google_calendar.oauth import GoogleCalendarOAuthCredentials
from plane.license.utils.google_calendar_credentials import google_calendar_credential_fingerprint
from plane.tests.factories import (
    GoogleCalendarConnectionFactory,
    IssueAssigneeFactory,
    IssueFactory,
    IssueLabelFactory,
    LabelFactory,
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
            integration__provider="google_calendar",
            config={"enabled": True, "mode": "assignment", "update_on_completion": True},
        )
        self.issue = IssueFactory(project__workspace=self.workspace_integration.workspace)
        self.connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            active=True,
            calendar_generation=5,
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
        assert (
            GoogleCalendarEvent.objects.get(connection=self.connection, entity_id=self.issue.id).calendar_generation
            == 5
        )
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

    @pytest.mark.parametrize("status_code", [404, 410])
    def test_insert_absence_requests_calendar_replacement_without_a_ledger_row(self, status_code):
        client = _provider_client()
        client.insert_event.side_effect = GoogleCalendarEventAbsent(f"provider returned {status_code}")

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as enqueue,
        ):
            result = synchronize_google_calendar_issue.run(str(self.issue.id), str(self.connection.id))

        assert result == ["replacement_pending"]
        self.connection.refresh_from_db()
        assert self.connection.status == "pending"
        assert not GoogleCalendarEvent.objects.filter(connection=self.connection, entity_id=self.issue.id).exists()
        assert enqueue.call_args.args[0].task == GOOGLE_CALENDAR_LIFECYCLE_TASK
        assert enqueue.call_args.args[1:] == (str(self.connection.id), self.connection.lifecycle_generation)

    def test_transient_update_conflict_then_tombstone_recreates_the_event(self):
        client = _provider_client()

        with patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client):
            assert synchronize_google_calendar_issue.run(str(self.issue.id)) == ["created"]
            client.reset_mock()
            client.access_token = None
            client.get_event.side_effect = [{"id": "existing-event"}, None]
            client.update_event.side_effect = GoogleCalendarClientConflict("transient conflict")
            self.issue.name = "Changed while provider conflicted"
            self.issue.save(update_fields=["name", "updated_at"])
            result = synchronize_google_calendar_issue.run(str(self.issue.id))

        assert result == ["updated"]
        assert client.get_event.call_count == 2
        client.insert_event.assert_called_once()
        assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 1

    def test_provider_retry_is_owned_by_the_client_and_commits_the_refreshed_token_once(self):
        credentials = GoogleCalendarOAuthCredentials("client-id", "client-secret")
        self.connection.access_token = "expired-access-token"
        self.connection.refresh_token = "refresh-token"
        self.connection.token_expires_at = timezone.now() - timedelta(minutes=1)
        self.connection.credential_fingerprint = google_calendar_credential_fingerprint(
            credentials.client_id,
            credentials.client_secret,
        )
        self.connection.save(
            update_fields=[
                "access_token",
                "refresh_token",
                "token_expires_at",
                "credential_fingerprint",
                "updated_at",
            ]
        )
        refresh_response = Mock(status_code=200)
        refresh_response.json.return_value = {"access_token": "fresh-access-token", "expires_in": 3600}
        failed_event_response = Mock(status_code=503)
        failed_event_response.raise_for_status.side_effect = requests.HTTPError("provider unavailable")
        successful_event_response = Mock(status_code=200)
        successful_event_response.json.return_value = {"id": "provider-event-id"}

        with (
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
                side_effect=[failed_event_response, successful_event_response],
            ) as request,
            patch("plane.integrations.google_calendar.client.random.uniform", return_value=0),
            patch("plane.integrations.google_calendar.client.time.sleep") as sleep,
            patch.object(synchronize_google_calendar_issue, "retry") as task_retry,
        ):
            result = synchronize_google_calendar_issue.run(str(self.issue.id), str(self.connection.id))

        self.connection.refresh_from_db()
        assert self.connection.access_token == "fresh-access-token"
        assert result == ["created"]
        assert GoogleCalendarEvent.objects.filter(connection=self.connection, entity_id=self.issue.id).exists()
        assert post.call_count == 1
        assert request.call_count == 2
        sleep.assert_called_once_with(1)
        task_retry.assert_not_called()

    def test_exhausted_client_retry_is_not_retried_again_by_the_worker(self):
        client = _provider_client()
        client.insert_event.side_effect = GoogleCalendarProviderError("provider retry budget exhausted")

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch.object(synchronize_google_calendar_issue, "retry") as task_retry,
            pytest.raises(GoogleCalendarProviderError, match="retry budget exhausted"),
        ):
            synchronize_google_calendar_issue.run(str(self.issue.id), str(self.connection.id))

        client.insert_event.assert_called_once()
        task_retry.assert_not_called()

    def test_multi_entity_fanout_advances_countdown_independently_per_connection(self):
        second_connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            member=UserFactory(),
            active=True,
        )
        issues = [self.issue, IssueFactory(project=self.issue.project), IssueFactory(project=self.issue.project)]
        for issue in issues:
            IssueAssigneeFactory(issue=issue, assignee=second_connection.member, project=issue.project)
        for issue in issues[1:]:
            IssueAssigneeFactory(issue=issue, assignee=self.connection.member, project=issue.project)

        tasks = list(paced_google_calendar_issue_sync_tasks([issue.id for issue in issues]))
        countdowns_by_connection = {}
        for task, _, connection_id in tasks:
            countdowns_by_connection.setdefault(connection_id, []).append(task.options["countdown"])

        assert countdowns_by_connection == {
            str(self.connection.id): [0, 1, 2],
            str(second_connection.id): [0, 1, 2],
        }

    def test_backfill_and_configuration_resync_pace_each_connection_independently(self):
        second_connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            member=UserFactory(),
            active=True,
        )
        issues = [self.issue, IssueFactory(project=self.issue.project), IssueFactory(project=self.issue.project)]
        IssueAssigneeFactory(issue=self.issue, assignee=second_connection.member, project=self.issue.project)
        for issue in issues[1:]:
            IssueAssigneeFactory(issue=issue, assignee=self.connection.member, project=issue.project)
            IssueAssigneeFactory(issue=issue, assignee=second_connection.member, project=issue.project)

        with patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as enqueue:
            backfill_google_calendar_open_issues.run(str(self.connection.id))
            backfill_google_calendar_open_issues.run(str(second_connection.id))

        backfill_countdowns = {}
        for queued in enqueue.call_args_list:
            task, _, connection_id = queued.args
            backfill_countdowns.setdefault(connection_id, []).append(task.options["countdown"])
        assert backfill_countdowns == {
            str(self.connection.id): [0, 1, 2],
            str(second_connection.id): [0, 1, 2],
        }

        for resync, resync_id in (
            (resync_google_calendar_project_issues, self.issue.project_id),
            (resync_google_calendar_workspace_issues, self.workspace_integration.workspace_id),
        ):
            with patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish:
                assert resync.run(str(resync_id)) == 3

            resync_countdowns = {}
            for queued in publish.call_args_list:
                task = queued.args[0]
                if task.task != GOOGLE_CALENDAR_ISSUE_SYNC_TASK:
                    continue
                connection_id = queued.args[2]
                resync_countdowns.setdefault(connection_id, []).append(task.options["countdown"])
            assert resync_countdowns == {
                str(self.connection.id): [0, 1, 2],
                str(second_connection.id): [0, 1, 2],
            }

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
            credential_fingerprint=google_calendar_credential_fingerprint("client-id", "client-secret"),
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )
        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=retry_client),
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=GoogleCalendarOAuthCredentials("client-id", "client-secret"),
            ),
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

    def test_state_resync_dispatches_only_matching_issues_in_keyset_pages(self):
        state = self.issue.state
        matching_issues = [
            self.issue,
            IssueFactory(project=self.issue.project, state=state),
            IssueFactory(project=self.issue.project, state=state),
        ]
        IssueFactory(project=self.issue.project)
        expected_ids = sorted((issue.id for issue in matching_issues), key=str)

        with (
            patch("plane.db.signals.dispatch_google_calendar_issue_syncs") as dispatch,
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
        ):
            published = resync_google_calendar_state_issues.run(str(state.id), batch_size=2)

        assert published == 2
        dispatch.assert_called_once_with(expected_ids[:2])
        publish.assert_called_once()
        continuation = publish.call_args
        assert continuation.args[0].task == GOOGLE_CALENDAR_STATE_ISSUE_RESYNC_TASK
        assert continuation.args[0].options["countdown"] == 2
        assert continuation.args[1:] == (
            str(state.id),
            str(expected_ids[1]),
            2,
        )

        with (
            patch("plane.db.signals.dispatch_google_calendar_issue_syncs") as dispatch,
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
        ):
            published = resync_google_calendar_state_issues.run(
                str(state.id),
                after_id=str(expected_ids[1]),
                batch_size=2,
            )

        assert published == 1
        dispatch.assert_called_once_with(expected_ids[2:])
        publish.assert_not_called()

    def test_state_resync_converges_saved_terminal_and_reopened_definitions(self):
        client = _provider_client()
        state = self.issue.state

        def synchronize_now(issue_ids):
            return [synchronize_google_calendar_issue.run(str(issue_id)) for issue_id in issue_ids]

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.db.signals.dispatch_google_calendar_issue_syncs", side_effect=synchronize_now),
        ):
            synchronize_google_calendar_issue.run(str(self.issue.id))

            state.name = "Shipped"
            state.group = "completed"
            state.save(update_fields=["name", "group", "updated_at"])
            completed_result = resync_google_calendar_state_issues.run(str(state.id))

            state.name = "Won't do"
            state.group = "cancelled"
            state.save(update_fields=["name", "group", "updated_at"])
            cancelled_result = resync_google_calendar_state_issues.run(str(state.id))

            state.name = "In progress again"
            state.group = "started"
            state.save(update_fields=["name", "group", "updated_at"])
            reopened_result = resync_google_calendar_state_issues.run(str(state.id))

        assert completed_result == 1
        assert cancelled_result == 1
        assert reopened_result == 1
        completed_payload, cancelled_payload, reopened_payload = [
            call.args[2] for call in client.update_event.call_args_list[-3:]
        ]
        assert completed_payload["summary"].startswith("[Completed]")
        assert completed_payload["colorId"] == "10"
        assert "State: Shipped" in completed_payload["description"]
        assert cancelled_payload["summary"].startswith("[Cancelled]")
        assert cancelled_payload["colorId"] == "10"
        assert "State: Won't do" in cancelled_payload["description"]
        assert reopened_payload["summary"].startswith(f"[{self.issue.project.identifier}-")
        assert "colorId" not in reopened_payload
        assert reopened_payload["status"] == "confirmed"
        assert "State: In progress again" in reopened_payload["description"]

    def test_state_resync_applies_delete_completion_policy_to_saved_group(self):
        client = _provider_client()
        state = self.issue.state

        def synchronize_now(issue_ids):
            return [synchronize_google_calendar_issue.run(str(issue_id)) for issue_id in issue_ids]

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.db.signals.dispatch_google_calendar_issue_syncs", side_effect=synchronize_now),
        ):
            synchronize_google_calendar_issue.run(str(self.issue.id))
            self.workspace_integration.config["update_on_completion"] = False
            self.workspace_integration.save(update_fields=["config", "updated_at"])
            state.group = "completed"
            state.save(update_fields=["group", "updated_at"])

            published = resync_google_calendar_state_issues.run(str(state.id))

        assert published == 1
        assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 0
        client.delete_event.assert_called_once()

    def test_label_resync_dispatches_distinct_linked_issues_in_keyset_pages(self):
        label = LabelFactory(project=self.issue.project)
        linked_issues = [self.issue, IssueFactory(project=self.issue.project), IssueFactory(project=self.issue.project)]
        relations = [IssueLabelFactory(issue=issue, label=label, project=self.issue.project) for issue in linked_issues]
        IssueLabelFactory(issue=self.issue, label=label, project=self.issue.project)
        other_label = LabelFactory(project=self.issue.project)
        IssueLabelFactory(issue=IssueFactory(project=self.issue.project), label=other_label, project=self.issue.project)
        IssueLabel.objects.filter(id=relations[1].id).delete()
        expected_ids = sorted((issue.id for issue in linked_issues), key=str)

        with (
            patch("plane.db.signals.dispatch_google_calendar_issue_syncs") as dispatch,
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
        ):
            published = resync_google_calendar_label.run(str(label.id), batch_size=2)

        assert published == 2
        dispatch.assert_called_once_with(expected_ids[:2])
        publish.assert_called_once()
        continuation = publish.call_args
        assert continuation.args[0].task == GOOGLE_CALENDAR_LABEL_ISSUE_RESYNC_TASK
        assert continuation.args[0].options["countdown"] == 2
        assert continuation.args[1:] == (str(label.id), str(expected_ids[1]), 2)

        with (
            patch("plane.db.signals.dispatch_google_calendar_issue_syncs") as dispatch,
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
        ):
            published = resync_google_calendar_label.run(
                str(label.id),
                after_id=str(expected_ids[1]),
                batch_size=2,
            )

        assert published == 1
        dispatch.assert_called_once_with(expected_ids[2:])
        publish.assert_not_called()

    def test_label_resync_converges_renamed_and_recursively_deleted_descriptions(self):
        client = _provider_client()
        label = LabelFactory(project=self.issue.project, name="Needs review")
        relation = IssueLabelFactory(issue=self.issue, label=label, project=self.issue.project)

        def synchronize_now(issue_ids):
            return [synchronize_google_calendar_issue.run(str(issue_id)) for issue_id in issue_ids]

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.db.signals.dispatch_google_calendar_issue_syncs", side_effect=synchronize_now),
        ):
            synchronize_google_calendar_issue.run(str(self.issue.id))

            label.name = "Customer request"
            label.save(update_fields=["name", "updated_at"])
            renamed_result = resync_google_calendar_label.run(str(label.id))
            repeated_renamed_result = resync_google_calendar_label.run(str(label.id))

            Label.all_objects.filter(id=label.id).update(deleted_at=timezone.now())
            IssueLabel.all_objects.filter(id=relation.id).update(deleted_at=timezone.now())
            deleted_result = resync_google_calendar_label.run(str(label.id))
            repeated_deleted_result = resync_google_calendar_label.run(str(label.id))

        assert renamed_result == 1
        assert repeated_renamed_result == 1
        assert deleted_result == 1
        assert repeated_deleted_result == 1
        assert client.update_event.call_count == 2
        renamed_payload, deleted_payload = [call.args[2] for call in client.update_event.call_args_list[-2:]]
        assert "Labels: Customer request" in renamed_payload["description"]
        assert "Needs review" not in renamed_payload["description"]
        assert deleted_payload["description"].endswith("Labels: ")
        assert "Customer request" not in deleted_payload["description"]

    def test_label_resync_rechecks_filter_eligibility_after_recursive_deletion(self):
        client = _provider_client()
        label = LabelFactory(project=self.issue.project)
        relation = IssueLabelFactory(issue=self.issue, label=label, project=self.issue.project)
        self.workspace_integration.config = {
            "enabled": True,
            "mode": "filter",
            "priorities": [],
            "label_ids": [str(label.id)],
            "label_match": "any",
        }
        self.workspace_integration.save(update_fields=["config", "updated_at"])

        def synchronize_now(issue_ids):
            return [synchronize_google_calendar_issue.run(str(issue_id)) for issue_id in issue_ids]

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.db.signals.dispatch_google_calendar_issue_syncs", side_effect=synchronize_now),
        ):
            synchronize_google_calendar_issue.run(str(self.issue.id))
            Label.all_objects.filter(id=label.id).update(deleted_at=timezone.now())
            IssueLabel.all_objects.filter(id=relation.id).update(deleted_at=timezone.now())
            published = resync_google_calendar_label.run(str(label.id))

        assert published == 1
        assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 0
        client.delete_event.assert_called_once()

    def test_project_resync_converges_committed_inclusion_and_archive_state(self):
        client = _provider_client()

        def synchronize_now(task, issue_id, connection_id):
            return synchronize_google_calendar_issue.run(issue_id, connection_id)

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch(
                "plane.bgtasks.google_calendar_task.publish_google_calendar_task",
                side_effect=synchronize_now,
            ),
        ):
            self.issue.project.google_calendar_sync_enabled = False
            self.issue.project.save(update_fields=["google_calendar_sync_enabled", "updated_at"])
            assert resync_google_calendar_project_issues.run(str(self.issue.project_id)) == 1
            assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 0

            self.issue.project.google_calendar_sync_enabled = True
            self.issue.project.save(update_fields=["google_calendar_sync_enabled", "updated_at"])
            assert resync_google_calendar_project_issues.run(str(self.issue.project_id)) == 1
            assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 1

            self.issue.project.google_calendar_sync_enabled = False
            self.issue.project.save(update_fields=["google_calendar_sync_enabled", "updated_at"])
            assert resync_google_calendar_project_issues.run(str(self.issue.project_id)) == 1
            assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 0

            self.issue.project.google_calendar_sync_enabled = True
            self.issue.project.save(update_fields=["google_calendar_sync_enabled", "updated_at"])
            assert resync_google_calendar_project_issues.run(str(self.issue.project_id)) == 1
            assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 1

            self.issue.project.archived_at = timezone.now()
            self.issue.project.save(update_fields=["archived_at", "updated_at"])
            assert resync_google_calendar_project_issues.run(str(self.issue.project_id)) == 1
            assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 0

            self.issue.project.archived_at = None
            self.issue.project.save(update_fields=["archived_at", "updated_at"])
            assert resync_google_calendar_project_issues.run(str(self.issue.project_id)) == 1

        assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 1
        assert client.insert_event.call_count == 3
        assert client.delete_event.call_count == 2

    def test_project_resync_reuses_assignee_and_correlation_fanout(self):
        second_connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            member=UserFactory(),
            active=True,
        )
        IssueAssigneeFactory(issue=self.issue, assignee=second_connection.member, project=self.issue.project)
        client = _provider_client()

        def synchronize_now(task, issue_id, connection_id):
            return synchronize_google_calendar_issue.run(issue_id, connection_id)

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch(
                "plane.bgtasks.google_calendar_task.publish_google_calendar_task",
                side_effect=synchronize_now,
            ),
        ):
            resync_google_calendar_project_issues.run(str(self.issue.project_id))
            self.issue.issue_assignee.get(assignee=second_connection.member).delete()
            resync_google_calendar_project_issues.run(str(self.issue.project_id))

        assert GoogleCalendarEvent.objects.filter(entity_id=self.issue.id).count() == 1
        assert GoogleCalendarEvent.objects.filter(connection=self.connection, entity_id=self.issue.id).exists()
        assert client.insert_event.call_count == 2
        client.delete_event.assert_called_once()

    def test_project_resync_is_project_scoped_and_paces_pages(self):
        second_issue = IssueFactory(project=self.issue.project)
        third_issue = IssueFactory(project=self.issue.project)
        foreign_issue = IssueFactory(project__workspace=self.workspace_integration.workspace)
        for issue in (second_issue, third_issue):
            IssueAssigneeFactory(issue=issue, assignee=self.connection.member, project=issue.project)

        with patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish:
            published = resync_google_calendar_project_issues.run(str(self.issue.project_id), batch_size=2)

        assert published == 2
        assert publish.call_count == 3
        first_sync, second_sync, continuation = publish.call_args_list
        assert {first_sync.args[1], second_sync.args[1]} < {
            str(self.issue.id),
            str(second_issue.id),
            str(third_issue.id),
        }
        assert str(foreign_issue.id) not in {first_sync.args[1], second_sync.args[1]}
        assert first_sync.args[0].options["countdown"] == 0
        assert second_sync.args[0].options["countdown"] == 1
        assert continuation.args[0].task == GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK
        assert continuation.args[0].options["countdown"] == 2
        assert continuation.args[1] == str(self.issue.project_id)
        assert continuation.args[3] == 2

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

        with patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish:
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
        for issue in (second_issue, third_issue):
            IssueAssigneeFactory(issue=issue, assignee=self.connection.member, project=issue.project)

        with patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish:
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

    def test_workspace_resync_clears_only_its_durable_generation_after_final_publication(self):
        generation = str(uuid4())
        self.workspace_integration.metadata = {
            "existing": "metadata",
            GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY: generation,
        }
        self.workspace_integration.save(update_fields=["metadata", "updated_at"])

        with patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish:
            published = resync_google_calendar_workspace_issues.run(
                self.workspace_integration.workspace_id,
                policy_generation=generation,
            )

        assert published == 1
        assert publish.call_args.args[0].task == GOOGLE_CALENDAR_ISSUE_SYNC_TASK
        self.workspace_integration.refresh_from_db()
        assert self.workspace_integration.metadata == {"existing": "metadata"}

    def test_workspace_resync_keeps_durable_generation_when_child_publication_fails(self):
        generation = str(uuid4())
        self.workspace_integration.metadata = {
            GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY: generation,
        }
        self.workspace_integration.save(update_fields=["metadata", "updated_at"])

        with (
            patch(
                "plane.bgtasks.google_calendar_task.publish_google_calendar_task",
                side_effect=RuntimeError("broker unavailable"),
            ),
            patch.object(resync_google_calendar_workspace_issues, "retry", side_effect=Retry()) as retry,
            pytest.raises(Retry),
        ):
            resync_google_calendar_workspace_issues.run(
                self.workspace_integration.workspace_id,
                policy_generation=generation,
            )

        retry.assert_called_once()
        self.workspace_integration.refresh_from_db()
        assert self.workspace_integration.metadata[GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY] == generation

    def test_reconciliation_rediscovers_durable_workspace_resync(self):
        generation = str(uuid4())
        self.workspace_integration.metadata = {
            GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY: generation,
        }
        self.workspace_integration.save(update_fields=["metadata", "updated_at"])

        with patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish:
            discovered = reconcile_google_calendar_workspace_issue_resyncs.run()

        assert discovered == 1
        task, workspace_id = publish.call_args.args
        assert task.task == GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK
        assert workspace_id == str(self.workspace_integration.workspace_id)
        assert publish.call_args.kwargs == {"policy_generation": generation}

    def test_reconciliation_republishes_pending_lifecycle_before_workspace_resync(self):
        generation = str(uuid4())
        self.workspace_integration.metadata = {
            GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY: generation,
        }
        self.workspace_integration.save(update_fields=["metadata", "updated_at"])
        self.connection.status = "pending"
        self.connection.lifecycle_generation = 4
        self.connection.save(update_fields=["status", "lifecycle_generation", "updated_at"])

        with patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish:
            discovered = reconcile_google_calendar_workspace_issue_resyncs.run()

        assert discovered == 1
        lifecycle_call, resync_call = publish.call_args_list
        assert lifecycle_call.args[0].task == GOOGLE_CALENDAR_LIFECYCLE_TASK
        assert lifecycle_call.args[1:] == (str(self.connection.id), 4)
        assert resync_call.args[0].task == GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK
        assert resync_call.args[1] == str(self.workspace_integration.workspace_id)
        assert resync_call.kwargs == {"policy_generation": generation}

    def test_workspace_resync_waits_for_policy_lifecycle_reconciliation(self):
        self.connection.status = "pending"
        self.connection.save(update_fields=["status", "updated_at"])

        with (
            patch.object(resync_google_calendar_workspace_issues, "retry", side_effect=Retry()) as retry,
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
            pytest.raises(Retry),
        ):
            resync_google_calendar_workspace_issues.run(self.workspace_integration.workspace_id)

        retry.assert_called_once_with(countdown=5)
        publish.assert_not_called()

    def test_successful_initial_provisioning_enqueues_open_item_and_cycle_backfills(self):
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
        assert [call.args for call in publish.call_args_list] == [
            (backfill_google_calendar_open_issues, str(connection.id)),
            (backfill_google_calendar_cycles, str(connection.id)),
        ]

    def test_cleanup_complete_reenable_enqueues_open_item_and_cycle_backfills(self):
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
        assert [call.args for call in publish.call_args_list] == [
            (backfill_google_calendar_open_issues, str(connection.id)),
            (backfill_google_calendar_cycles, str(connection.id)),
        ]

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
def test_workspace_resync_reconciliation_has_a_periodic_production_entrypoint():
    schedule = celery_app.conf.beat_schedule["reconcile-google-calendar-workspace-issue-resyncs"]

    assert schedule["task"] == GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_RECONCILIATION_TASK


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
