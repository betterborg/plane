# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone
from rest_framework import status

from plane.bgtasks.google_calendar_task import (
    synchronize_google_calendar_cycle,
    synchronize_google_calendar_issue,
)
from plane.db.models import CycleIssue, DraftIssue, GoogleCalendarEvent, IssueAssignee
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_CYCLE_SYNC_TASK,
    GOOGLE_CALENDAR_ISSUE_SYNC_TASK,
)
from plane.tests.contract.app.google_calendar_helpers import targeted_task
from plane.tests.factories import (
    CycleFactory,
    GoogleCalendarConnectionFactory,
    ProjectFactory,
    ProjectMemberFactory,
    StateFactory,
    WorkspaceIntegrationFactory,
)


def _draft_conversion_url(workspace, draft):
    return f"/api/workspaces/{workspace.slug}/draft-to-issue/{draft.id}/"


def _capture_on_commit(callbacks):
    def capture(callback, using=None, robust=False):
        callbacks.append((callback, robust))

    return capture


def _run_callbacks(callbacks):
    assert all(robust is True for _, robust in callbacks)
    for callback, _ in callbacks:
        callback()


def _conversion_setup(workspace, create_user, *, project_sync_enabled=True, ended_cycle=False):
    project = ProjectFactory(
        workspace=workspace,
        google_calendar_sync_enabled=project_sync_enabled,
    )
    ProjectMemberFactory(project=project, member=create_user)
    state = StateFactory(project=project)
    draft = DraftIssue.objects.create(
        name="Draft work item",
        project=project,
        state=state,
        created_by=create_user,
    )
    if ended_cycle:
        cycle = CycleFactory(
            project=project,
            start_date=timezone.now() - timedelta(days=14),
            end_date=timezone.now() - timedelta(days=7),
        )
    else:
        cycle = CycleFactory(project=project)
    return project, state, draft, cycle


def _conversion_payload(state, create_user, *, cycle=None):
    payload = {
        "name": "Converted work item",
        "state_id": str(state.id),
        "assignee_ids": [str(create_user.id)],
    }
    if cycle is not None:
        payload["cycle_id"] = str(cycle.id)
    return payload


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarDraftCycleDispatch:
    def test_conversion_dispatches_cycle_once_after_membership_and_assignees_are_queryable(
        self,
        session_client,
        workspace,
        create_user,
    ):
        project, state, draft, cycle = _conversion_setup(workspace, create_user)
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration__provider="google_calendar",
            config={"enabled": True, "recipients": "cycle_members"},
        )
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            member=create_user,
            active=True,
        )
        provider_client = Mock()
        provider_client.access_token = None
        provider_client.list_events.return_value = []
        callbacks = []
        observed_cycle_ids = []

        def converge_from_final_state(cycle_id, connection_id):
            assert connection_id == str(connection.id)
            membership = CycleIssue.objects.get(cycle_id=cycle_id)
            assert IssueAssignee.objects.filter(
                issue_id=membership.issue_id,
                assignee=create_user,
            ).exists()
            observed_cycle_ids.append(cycle_id)
            return synchronize_google_calendar_cycle.run(cycle_id, connection_id)

        targeted_issue_task = targeted_task()
        targeted_cycle_task = targeted_task(converge_from_final_state)

        def targeted_signature(task_name):
            if task_name == GOOGLE_CALENDAR_ISSUE_SYNC_TASK:
                return targeted_issue_task
            if task_name == GOOGLE_CALENDAR_CYCLE_SYNC_TASK:
                return targeted_cycle_task
            raise AssertionError(f"Unexpected task: {task_name}")

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=provider_client),
            patch(
                "plane.bgtasks.google_calendar_task.current_app.signature",
                side_effect=targeted_signature,
            ) as signature,
            patch(
                "plane.integrations.google_calendar.dispatch.transaction.on_commit",
                side_effect=_capture_on_commit(callbacks),
            ),
            patch.object(synchronize_google_calendar_issue, "delay") as fallback_issue_dispatch,
            patch.object(synchronize_google_calendar_cycle, "delay") as fallback_cycle_dispatch,
            patch("plane.app.views.workspace.draft.issue_activity.delay"),
            patch("plane.db.mixins.soft_delete_related_objects.delay"),
        ):
            response = session_client.post(
                _draft_conversion_url(workspace, draft),
                _conversion_payload(state, create_user, cycle=cycle),
                format="json",
            )

            assert response.status_code == status.HTTP_201_CREATED, response.data
            assert observed_cycle_ids == []
            assert len(callbacks) == 3
            _run_callbacks(callbacks)

        assert {call.args[0] for call in signature.call_args_list} == {
            GOOGLE_CALENDAR_ISSUE_SYNC_TASK,
            GOOGLE_CALENDAR_CYCLE_SYNC_TASK,
        }
        targeted_issue_task.set.assert_called_once_with(countdown=0)
        targeted_issue_task.delay.assert_called_once_with(str(response.data["id"]), str(connection.id))
        assert [invocation.kwargs for invocation in targeted_cycle_task.set.call_args_list] == [
            {"countdown": 0},
            {"countdown": 0},
        ]
        targeted_cycle_task.delay.assert_called_once_with(str(cycle.id), str(connection.id))
        fallback_issue_dispatch.assert_not_called()
        fallback_cycle_dispatch.assert_not_called()
        assert observed_cycle_ids == [str(cycle.id)]
        assert (
            GoogleCalendarEvent.objects.filter(
                entity_type=GoogleCalendarEvent.EntityType.CYCLE,
                entity_id=cycle.id,
            ).count()
            == 1
        )

    def test_conversion_without_cycle_schedules_no_cycle_work(
        self,
        session_client,
        workspace,
        create_user,
    ):
        _, state, draft, _ = _conversion_setup(workspace, create_user)
        callbacks = []

        with (
            patch(
                "plane.integrations.google_calendar.dispatch.transaction.on_commit",
                side_effect=_capture_on_commit(callbacks),
            ),
            patch.object(synchronize_google_calendar_issue, "delay") as issue_dispatch,
            patch.object(synchronize_google_calendar_cycle, "delay") as cycle_dispatch,
            patch("plane.app.views.workspace.draft.issue_activity.delay"),
            patch("plane.db.mixins.soft_delete_related_objects.delay"),
        ):
            response = session_client.post(
                _draft_conversion_url(workspace, draft),
                _conversion_payload(state, create_user),
                format="json",
            )

            assert response.status_code == status.HTTP_201_CREATED, response.data
            assert len(callbacks) == 2
            _run_callbacks(callbacks)

        issue_dispatch.assert_called_once_with(str(response.data["id"]))
        cycle_dispatch.assert_not_called()
        assert not CycleIssue.objects.filter(issue_id=response.data["id"]).exists()

    @pytest.mark.parametrize(
        ("project_sync_enabled", "ended_cycle"),
        ((False, False), (True, True)),
        ids=("excluded-project", "ended-cycle"),
    )
    def test_dispatched_ineligible_cycle_creates_no_recipient_block(
        self,
        session_client,
        workspace,
        create_user,
        project_sync_enabled,
        ended_cycle,
    ):
        _, state, draft, cycle = _conversion_setup(
            workspace,
            create_user,
            project_sync_enabled=project_sync_enabled,
            ended_cycle=ended_cycle,
        )
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration__provider="google_calendar",
            config={"enabled": True, "recipients": "cycle_members"},
        )
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            member=create_user,
            active=True,
        )
        callbacks = []
        targeted_issue_task = targeted_task()

        with (
            patch(
                "plane.bgtasks.google_calendar_task.current_app.signature",
                return_value=targeted_issue_task,
            ) as signature,
            patch(
                "plane.integrations.google_calendar.dispatch.transaction.on_commit",
                side_effect=_capture_on_commit(callbacks),
            ),
            patch.object(synchronize_google_calendar_issue, "delay") as fallback_issue_dispatch,
            patch.object(
                synchronize_google_calendar_cycle,
                "delay",
                side_effect=synchronize_google_calendar_cycle.run,
            ) as cycle_dispatch,
            patch("plane.app.views.workspace.draft.issue_activity.delay"),
            patch("plane.db.mixins.soft_delete_related_objects.delay"),
        ):
            response = session_client.post(
                _draft_conversion_url(workspace, draft),
                _conversion_payload(state, create_user, cycle=cycle),
                format="json",
            )

            assert response.status_code == status.HTTP_201_CREATED, response.data
            assert len(callbacks) == 3
            _run_callbacks(callbacks)

        signature.assert_called_once_with(GOOGLE_CALENDAR_ISSUE_SYNC_TASK)
        targeted_issue_task.set.assert_called_once_with(countdown=0)
        targeted_issue_task.delay.assert_called_once_with(str(response.data["id"]), str(connection.id))
        fallback_issue_dispatch.assert_not_called()
        cycle_dispatch.assert_called_once_with(str(cycle.id))
        assert not GoogleCalendarEvent.objects.filter(
            entity_type=GoogleCalendarEvent.EntityType.CYCLE,
            entity_id=cycle.id,
        ).exists()
