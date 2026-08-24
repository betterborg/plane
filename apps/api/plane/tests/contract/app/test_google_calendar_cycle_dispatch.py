# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from plane.app.serializers.issue import IssueCreateSerializer
from plane.bgtasks.google_calendar_task import (
    synchronize_google_calendar_cycle,
    synchronize_google_calendar_issue,
)
from plane.db.models import Cycle, GoogleCalendarEvent, IssueAssignee
from plane.db.signals import suppress_google_calendar_issue_signal_dispatch
from plane.integrations.google_calendar.dispatch import GOOGLE_CALENDAR_CYCLE_SYNC_TASK
from plane.tests.factories import (
    CycleFactory,
    CycleIssueFactory,
    GoogleCalendarConnectionFactory,
    IssueAssigneeFactory,
    IssueFactory,
    ProjectFactory,
    ProjectMemberFactory,
    UserFactory,
    WorkspaceIntegrationFactory,
)


def _provider_client():
    client = Mock()
    client.access_token = None
    client.get_event.return_value = {"id": "existing-event"}
    client.list_events.return_value = []
    return client


def _targeted_task(side_effect):
    task = Mock(options={})

    def set_options(**options):
        task.options.update(options)
        return task

    task.set.side_effect = set_options
    task.delay.side_effect = side_effect
    return task


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarCycleDispatch:
    def test_cycle_lifecycle_mutations_converge_from_persisted_state_without_duplicates(self):
        workspace_integration = WorkspaceIntegrationFactory(
            integration__provider="google_calendar",
            config={"enabled": True, "recipients": "cycle_members"},
        )
        cycle = CycleFactory(project__workspace=workspace_integration.workspace)
        with suppress_google_calendar_issue_signal_dispatch():
            issue = IssueFactory(project=cycle.project, target_date=None)
            CycleIssueFactory(cycle=cycle, issue=issue, project=cycle.project)
            connection = GoogleCalendarConnectionFactory(
                workspace_integration=workspace_integration,
                active=True,
            )
            IssueAssigneeFactory(issue=issue, assignee=connection.member, project=cycle.project)

        client = _provider_client()
        callbacks = []
        observed_states = []

        def capture_on_commit(callback, robust=False):
            callbacks.append((callback, robust))

        def synchronize_persisted_cycle(cycle_id, connection_id):
            assert connection_id == str(connection.id)
            saved_cycle = Cycle.all_objects.get(id=cycle_id)
            observed_states.append(
                {
                    "name": saved_cycle.name,
                    "start_date": saved_cycle.start_date,
                    "end_date": saved_cycle.end_date,
                    "owned_by_id": saved_cycle.owned_by_id,
                    "archived_at": saved_cycle.archived_at,
                    "deleted_at": saved_cycle.deleted_at,
                }
            )
            return synchronize_google_calendar_cycle.run(cycle_id, connection_id)

        def save_and_converge(update_fields):
            cycle.save(update_fields=[*update_fields, "updated_at"])
            assert len(callbacks) == 1
            callback, robust = callbacks.pop()
            assert robust is True
            callback()
            assert (
                GoogleCalendarEvent.objects.filter(
                    entity_type=GoogleCalendarEvent.EntityType.CYCLE,
                    entity_id=cycle.id,
                ).count()
                <= 1
            )

        targeted_cycle_task = _targeted_task(synchronize_persisted_cycle)

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch(
                "plane.bgtasks.google_calendar_task.current_app.signature",
                return_value=targeted_cycle_task,
            ) as signature,
            patch(
                "plane.integrations.google_calendar.dispatch.transaction.on_commit",
                side_effect=capture_on_commit,
            ),
            patch.object(
                synchronize_google_calendar_cycle,
                "delay",
            ) as fallback_cycle_dispatch,
            patch("plane.db.mixins.soft_delete_related_objects.delay"),
        ):
            assert synchronize_google_calendar_cycle.run(str(cycle.id)) == ["created"]

            cycle.start_date -= timedelta(days=1)
            cycle.end_date += timedelta(days=1)
            save_and_converge(["start_date", "end_date"])

            cycle.name = "Release train"
            save_and_converge(["name"])

            new_owner = UserFactory()
            cycle.owned_by = new_owner
            save_and_converge(["owned_by"])

            cycle.archived_at = timezone.now()
            save_and_converge(["archived_at"])
            assert not GoogleCalendarEvent.objects.filter(entity_id=cycle.id).exists()

            cycle.archived_at = None
            save_and_converge(["archived_at"])

            cycle.delete()
            assert len(callbacks) == 1
            callback, robust = callbacks.pop()
            assert robust is True
            callback()
            assert not GoogleCalendarEvent.objects.filter(entity_id=cycle.id).exists()

            cycle.deleted_at = None
            save_and_converge(["deleted_at"])

        assert len(observed_states) == 7
        assert observed_states[0]["start_date"] == cycle.start_date
        assert observed_states[0]["end_date"] == cycle.end_date
        assert observed_states[1]["name"] == "Release train"
        assert observed_states[2]["owned_by_id"] == new_owner.id
        assert observed_states[3]["archived_at"] is not None
        assert observed_states[4]["archived_at"] is None
        assert observed_states[5]["deleted_at"] is not None
        assert observed_states[6]["deleted_at"] is None
        assert GoogleCalendarEvent.objects.filter(entity_id=cycle.id).count() == 1
        assert client.insert_event.call_count == 3
        assert client.update_event.call_count == 2
        assert client.delete_event.call_count == 2
        assert [invocation.args for invocation in signature.call_args_list] == [(GOOGLE_CALENDAR_CYCLE_SYNC_TASK,)] * 7
        assert [invocation.kwargs for invocation in targeted_cycle_task.set.call_args_list] == [
            {"countdown": 0},
            {"countdown": 0},
        ] * 7
        assert [invocation.args for invocation in targeted_cycle_task.delay.call_args_list] == [
            (str(cycle.id), str(connection.id))
        ] * 7
        fallback_cycle_dispatch.assert_not_called()

    def test_assignee_replacement_dispatches_current_cycle_after_final_relations(self, workspace, create_user):
        project = ProjectFactory(workspace=workspace)
        ProjectMemberFactory(project=project, member=create_user)
        new_assignee = UserFactory()
        ProjectMemberFactory(project=project, member=new_assignee)
        cycle = CycleFactory(project=project)
        with suppress_google_calendar_issue_signal_dispatch():
            issue = IssueFactory(project=project)
            CycleIssueFactory(cycle=cycle, issue=issue, project=project)
            IssueAssigneeFactory(issue=issue, assignee=create_user, project=project)

        serializer = IssueCreateSerializer(
            issue,
            data={"assignee_ids": [str(new_assignee.id)]},
            partial=True,
            context={"project_id": project.id},
        )
        assert serializer.is_valid(), serializer.errors

        callbacks = []
        observed_assignees = []

        def capture_on_commit(callback, robust=False):
            callbacks.append((callback, robust))

        def inspect_final_assignees(cycle_id):
            assert str(cycle_id) == str(cycle.id)
            observed_assignees.append(
                set(IssueAssignee.objects.filter(issue=issue).values_list("assignee_id", flat=True))
            )

        with (
            patch(
                "plane.integrations.google_calendar.dispatch.transaction.on_commit",
                side_effect=capture_on_commit,
            ),
            patch.object(synchronize_google_calendar_issue, "delay"),
            patch.object(
                synchronize_google_calendar_cycle,
                "delay",
                side_effect=inspect_final_assignees,
            ),
        ):
            serializer.save()
            assert observed_assignees == []
            assert len(callbacks) == 2
            assert all(robust is True for _, robust in callbacks)
            for callback, _ in callbacks:
                callback()

        assert observed_assignees == [{new_assignee.id}]
