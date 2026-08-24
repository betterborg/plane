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
    def capture(callback, robust=False):
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
        GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            member=create_user,
            active=True,
        )
        provider_client = Mock()
        provider_client.access_token = None
        provider_client.list_events.return_value = []
        callbacks = []
        observed_cycle_ids = []

        def converge_from_final_state(cycle_id):
            membership = CycleIssue.objects.get(cycle_id=cycle_id)
            assert IssueAssignee.objects.filter(
                issue_id=membership.issue_id,
                assignee=create_user,
            ).exists()
            observed_cycle_ids.append(cycle_id)
            return synchronize_google_calendar_cycle.run(cycle_id)

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=provider_client),
            patch(
                "plane.integrations.google_calendar.dispatch.transaction.on_commit",
                side_effect=_capture_on_commit(callbacks),
            ),
            patch.object(synchronize_google_calendar_issue, "delay") as issue_dispatch,
            patch.object(
                synchronize_google_calendar_cycle,
                "delay",
                side_effect=converge_from_final_state,
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
            assert observed_cycle_ids == []
            assert len(callbacks) == 2
            _run_callbacks(callbacks)

        issue_dispatch.assert_called_once_with(str(response.data["id"]))
        cycle_dispatch.assert_called_once_with(str(cycle.id))
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
            assert len(callbacks) == 1
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
        GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            member=create_user,
            active=True,
        )
        callbacks = []

        with (
            patch(
                "plane.integrations.google_calendar.dispatch.transaction.on_commit",
                side_effect=_capture_on_commit(callbacks),
            ),
            patch.object(synchronize_google_calendar_issue, "delay"),
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
            assert len(callbacks) == 2
            _run_callbacks(callbacks)

        cycle_dispatch.assert_called_once_with(str(cycle.id))
        assert not GoogleCalendarEvent.objects.filter(
            entity_type=GoogleCalendarEvent.EntityType.CYCLE,
            entity_id=cycle.id,
        ).exists()
