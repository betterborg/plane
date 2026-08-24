# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from plane.bgtasks.google_calendar_task import (
    backfill_google_calendar_cycles,
    backfill_google_calendar_open_issues,
    reconcile_google_calendar_connection,
    resync_google_calendar_project_cycles,
    resync_google_calendar_project_issues,
    synchronize_google_calendar_cycle,
)
from plane.db.models import GoogleCalendarConnection, GoogleCalendarEvent, IssueAssignee
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_CYCLE_SYNC_TASK,
    GOOGLE_CALENDAR_PROJECT_CYCLE_RESYNC_TASK,
)
from plane.tests.factories import (
    CycleFactory,
    CycleIssueFactory,
    GoogleCalendarConnectionFactory,
    IssueAssigneeFactory,
    IssueFactory,
    UserFactory,
    WorkspaceIntegrationFactory,
)


def _provider_client():
    client = Mock()
    client.access_token = None
    client.get_event.return_value = {"id": "existing-event"}
    client.list_events.return_value = []
    client.find_calendar.return_value = None
    client.create_calendar.return_value = "new-plane-calendar"
    return client


@pytest.mark.unit
@pytest.mark.django_db
class TestGoogleCalendarCycleTask:
    def setup_method(self):
        self.workspace_integration = WorkspaceIntegrationFactory(
            integration__provider="google_calendar",
            config={"enabled": True, "recipients": "cycle_members"},
        )
        self.cycle = CycleFactory(project__workspace=self.workspace_integration.workspace)
        self.issue = IssueFactory(project=self.cycle.project, target_date=None)
        CycleIssueFactory(cycle=self.cycle, issue=self.issue, project=self.cycle.project)
        self.connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            active=True,
        )
        IssueAssigneeFactory(issue=self.issue, assignee=self.connection.member, project=self.cycle.project)

    def test_fans_out_to_cycle_members_without_duplicates(self):
        second_connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            member=UserFactory(),
            active=True,
        )
        IssueAssigneeFactory(issue=self.issue, assignee=second_connection.member, project=self.cycle.project)
        client = _provider_client()

        with patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client):
            first_result = synchronize_google_calendar_cycle.run(str(self.cycle.id))
            second_result = synchronize_google_calendar_cycle.run(str(self.cycle.id))

        assert first_result == ["created", "created"]
        assert second_result == ["unchanged", "unchanged"]
        assert (
            GoogleCalendarEvent.objects.filter(
                entity_type=GoogleCalendarEvent.EntityType.CYCLE,
                entity_id=self.cycle.id,
            ).count()
            == 2
        )
        assert client.insert_event.call_count == 2

    def test_recipient_removal_archive_and_deletion_remove_marked_blocks(self):
        client = _provider_client()
        with patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client):
            assert synchronize_google_calendar_cycle.run(str(self.cycle.id)) == ["created"]
            IssueAssignee.objects.filter(issue=self.issue, assignee=self.connection.member).delete()
            assert synchronize_google_calendar_cycle.run(str(self.cycle.id)) == ["deleted"]

            IssueAssigneeFactory(issue=self.issue, assignee=self.connection.member, project=self.cycle.project)
            assert synchronize_google_calendar_cycle.run(str(self.cycle.id)) == ["created"]
            self.cycle.archived_at = timezone.now()
            self.cycle.save(update_fields=["archived_at", "updated_at"])
            assert synchronize_google_calendar_cycle.run(str(self.cycle.id)) == ["deleted"]

            self.cycle.archived_at = None
            self.cycle.save(update_fields=["archived_at", "updated_at"])
            assert synchronize_google_calendar_cycle.run(str(self.cycle.id)) == ["created"]
            cycle_id = self.cycle.id
            self.cycle.delete(soft=False)
            assert synchronize_google_calendar_cycle.run(str(cycle_id)) == "missing"

        assert not GoogleCalendarEvent.objects.filter(
            entity_type=GoogleCalendarEvent.EntityType.CYCLE,
            entity_id=self.cycle.id,
        ).exists()
        assert client.delete_event.call_count == 3

    def test_naturally_ended_blocks_remain_history_but_are_not_added_to_new_connections(self):
        client = _provider_client()
        with patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client):
            assert synchronize_google_calendar_cycle.run(str(self.cycle.id)) == ["created"]
            self.cycle.end_date = timezone.now() - timedelta(days=2)
            self.cycle.start_date = self.cycle.end_date - timedelta(days=7)
            self.cycle.save(update_fields=["start_date", "end_date", "updated_at"])
            second_connection = GoogleCalendarConnectionFactory(
                workspace_integration=self.workspace_integration,
                member=UserFactory(),
                active=True,
            )
            IssueAssigneeFactory(issue=self.issue, assignee=second_connection.member, project=self.cycle.project)
            result = synchronize_google_calendar_cycle.run(str(self.cycle.id))

        assert sorted(result) == ["ineligible", "retained"]
        assert (
            GoogleCalendarEvent.objects.filter(
                entity_type=GoogleCalendarEvent.EntityType.CYCLE,
                entity_id=self.cycle.id,
            ).count()
            == 1
        )
        assert client.insert_event.call_count == 1
        client.update_event.assert_not_called()

    def test_backfill_publishes_active_and_upcoming_but_not_ended_cycles(self):
        upcoming = CycleFactory(
            project=self.cycle.project,
            start_date=timezone.now() + timedelta(days=10),
            end_date=timezone.now() + timedelta(days=17),
        )
        ended = CycleFactory(
            project=self.cycle.project,
            start_date=timezone.now() - timedelta(days=17),
            end_date=timezone.now() - timedelta(days=10),
        )
        for cycle in (upcoming, ended):
            issue = IssueFactory(project=self.cycle.project, target_date=None)
            CycleIssueFactory(cycle=cycle, issue=issue, project=self.cycle.project)
            IssueAssigneeFactory(issue=issue, assignee=self.connection.member, project=self.cycle.project)

        with patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as publish:
            published = backfill_google_calendar_cycles.run(str(self.connection.id))

        assert published == 2
        assert {call.args[1] for call in publish.call_args_list} == {str(self.cycle.id), str(upcoming.id)}
        assert str(ended.id) not in {call.args[1] for call in publish.call_args_list}

    def test_project_resync_removes_and_recreates_current_cycle_blocks(self):
        client = _provider_client()

        def publish_immediately(task, *args, **kwargs):
            if task.task == GOOGLE_CALENDAR_PROJECT_CYCLE_RESYNC_TASK:
                return resync_google_calendar_project_cycles.run(*args, **kwargs)
            if task.task == GOOGLE_CALENDAR_CYCLE_SYNC_TASK:
                return synchronize_google_calendar_cycle.run(*args, **kwargs)
            return None

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch(
                "plane.bgtasks.google_calendar_task.publish_google_calendar_task",
                side_effect=publish_immediately,
            ),
        ):
            synchronize_google_calendar_cycle.run(str(self.cycle.id))
            for included, archived, expected in (
                (False, None, False),
                (True, None, True),
                (True, timezone.now(), False),
                (True, None, True),
            ):
                self.cycle.project.google_calendar_sync_enabled = included
                self.cycle.project.archived_at = archived
                self.cycle.project.save(update_fields=["google_calendar_sync_enabled", "archived_at", "updated_at"])
                resync_google_calendar_project_issues.run(str(self.cycle.project_id))
                assert (
                    GoogleCalendarEvent.objects.filter(
                        connection=self.connection,
                        entity_type=GoogleCalendarEvent.EntityType.CYCLE,
                        entity_id=self.cycle.id,
                    ).exists()
                    is expected
                )

        assert client.insert_event.call_count == 3
        assert client.delete_event.call_count == 2

    def test_successful_provisioning_enqueues_work_item_and_cycle_backfills(self):
        pending_connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
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
            patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as enqueue,
        ):
            result = reconcile_google_calendar_connection.run(str(pending_connection.id), 3)

        assert result == "active"
        assert [call.args[0] for call in enqueue.call_args_list] == [
            backfill_google_calendar_open_issues,
            backfill_google_calendar_cycles,
        ]
