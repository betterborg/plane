# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone
from rest_framework import status

from plane.bgtasks.google_calendar_task import synchronize_google_calendar_cycle
from plane.db.models import CycleIssue, GoogleCalendarEvent
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
    WorkspaceIntegrationFactory,
)


def _cycle_issue_url(api_prefix, workspace, project, cycle, issue=None):
    url = f"{api_prefix}/workspaces/{workspace.slug}/projects/{project.id}/cycles/{cycle.id}/cycle-issues/"
    return f"{url}{issue.id}/" if issue is not None else url


def _transfer_url(api_prefix, workspace, project, cycle):
    return f"{api_prefix}/workspaces/{workspace.slug}/projects/{project.id}/cycles/{cycle.id}/transfer-issues/"


def _capture_on_commit(callbacks):
    def capture(callback, robust=False):
        callbacks.append((callback, robust))

    return capture


def _run_callbacks(callbacks):
    assert all(robust is True for _, robust in callbacks)
    for callback, _ in callbacks:
        callback()


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
class TestGoogleCalendarCycleMembershipDispatch:
    @pytest.mark.parametrize(
        ("client_fixture", "api_prefix"),
        (("session_client", "/api"), ("api_key_client", "/api/v1")),
    )
    def test_bulk_add_and_update_dispatch_destination_and_old_cycle_from_final_state(
        self,
        request,
        client_fixture,
        api_prefix,
        workspace,
        create_user,
    ):
        client = request.getfixturevalue(client_fixture)
        project = ProjectFactory(workspace=workspace)
        ProjectMemberFactory(project=project, member=create_user)
        old_cycle = CycleFactory(project=project)
        destination_cycle = CycleFactory(project=project)
        moved_issue = IssueFactory(project=project)
        added_issue = IssueFactory(project=project)
        with suppress_google_calendar_issue_signal_dispatch():
            CycleIssueFactory(cycle=old_cycle, issue=moved_issue, project=project)

        callbacks = []
        observed_memberships = []

        def inspect_final_memberships(cycle_id):
            observed_memberships.append(
                (
                    cycle_id,
                    set(
                        CycleIssue.objects.filter(issue_id__in=[moved_issue.id, added_issue.id]).values_list(
                            "cycle_id", flat=True
                        )
                    ),
                )
            )

        with (
            patch(
                "plane.integrations.google_calendar.dispatch.transaction.on_commit",
                side_effect=_capture_on_commit(callbacks),
            ),
            patch.object(synchronize_google_calendar_cycle, "delay", side_effect=inspect_final_memberships),
            patch("plane.app.views.cycle.issue.issue_activity.delay"),
            patch("plane.api.views.cycle.issue_activity.delay"),
        ):
            response = client.post(
                _cycle_issue_url(api_prefix, workspace, project, destination_cycle),
                {"issues": [str(moved_issue.id), str(added_issue.id)]},
                format="json",
            )

            assert response.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED)
            assert observed_memberships == []
            assert len(callbacks) == 2
            _run_callbacks(callbacks)

        assert {str(cycle_id) for cycle_id, _ in observed_memberships} == {
            str(old_cycle.id),
            str(destination_cycle.id),
        }
        assert all(memberships == {destination_cycle.id} for _, memberships in observed_memberships)

    @pytest.mark.parametrize(
        ("client_fixture", "api_prefix"),
        (("session_client", "/api"), ("api_key_client", "/api/v1")),
    )
    def test_transfer_endpoint_dispatches_source_and_destination_after_bulk_update_once(
        self,
        request,
        client_fixture,
        api_prefix,
        workspace,
        create_user,
    ):
        client = request.getfixturevalue(client_fixture)
        project = ProjectFactory(workspace=workspace)
        ProjectMemberFactory(project=project, member=create_user)
        source_cycle = CycleFactory(
            project=project,
            start_date=timezone.now() - timedelta(days=14),
            end_date=timezone.now() - timedelta(days=7),
        )
        destination_cycle = CycleFactory(project=project)
        issue = IssueFactory(project=project)
        with suppress_google_calendar_issue_signal_dispatch():
            CycleIssueFactory(cycle=source_cycle, issue=issue, project=project)

        callbacks = []
        observed_cycles = []

        def inspect_final_membership(cycle_id):
            observed_cycles.append(cycle_id)
            assert CycleIssue.objects.get(issue=issue).cycle_id == destination_cycle.id

        with (
            patch(
                "plane.integrations.google_calendar.dispatch.transaction.on_commit",
                side_effect=_capture_on_commit(callbacks),
            ),
            patch.object(synchronize_google_calendar_cycle, "delay", side_effect=inspect_final_membership),
            patch("plane.utils.cycle_transfer_issues.burndown_plot", return_value={}),
            patch("plane.utils.cycle_transfer_issues.issue_activity.delay"),
        ):
            response = client.post(
                _transfer_url(api_prefix, workspace, project, source_cycle),
                {"new_cycle_id": str(destination_cycle.id)},
                format="json",
            )

            assert response.status_code == status.HTTP_200_OK
            assert observed_cycles == []
            assert len(callbacks) == 2
            _run_callbacks(callbacks)

        assert {str(cycle_id) for cycle_id in observed_cycles} == {
            str(source_cycle.id),
            str(destination_cycle.id),
        }

    @pytest.mark.parametrize(
        ("client_fixture", "api_prefix"),
        (("session_client", "/api"), ("api_key_client", "/api/v1")),
    )
    def test_removing_last_membership_dispatches_after_delete_and_removes_obsolete_block(
        self,
        request,
        client_fixture,
        api_prefix,
        workspace,
        create_user,
    ):
        client = request.getfixturevalue(client_fixture)
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration__provider="google_calendar",
            config={"enabled": True, "recipients": "cycle_members"},
        )
        project = ProjectFactory(workspace=workspace)
        ProjectMemberFactory(project=project, member=create_user)
        cycle = CycleFactory(project=project)
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            active=True,
        )
        with suppress_google_calendar_issue_signal_dispatch():
            issue = IssueFactory(project=project, target_date=None)
            CycleIssueFactory(cycle=cycle, issue=issue, project=project)
            IssueAssigneeFactory(issue=issue, assignee=connection.member, project=project)

        provider_client = Mock()
        provider_client.access_token = None
        provider_client.list_events.return_value = []
        callbacks = []

        def converge_targeted_cycle(cycle_id, connection_id):
            assert connection_id == str(connection.id)
            return synchronize_google_calendar_cycle.run(cycle_id, connection_id)

        targeted_cycle_task = _targeted_task(converge_targeted_cycle)

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=provider_client),
            patch(
                "plane.bgtasks.google_calendar_task.current_app.signature",
                return_value=targeted_cycle_task,
            ) as signature,
            patch(
                "plane.integrations.google_calendar.dispatch.transaction.on_commit",
                side_effect=_capture_on_commit(callbacks),
            ),
            patch.object(
                synchronize_google_calendar_cycle,
                "delay",
            ) as fallback_cycle_dispatch,
            patch("plane.app.views.cycle.issue.issue_activity.delay"),
            patch("plane.api.views.cycle.issue_activity.delay"),
        ):
            assert synchronize_google_calendar_cycle.run(str(cycle.id)) == ["created"]
            assert GoogleCalendarEvent.objects.filter(entity_id=cycle.id).count() == 1

            response = client.delete(_cycle_issue_url(api_prefix, workspace, project, cycle, issue))

            assert response.status_code == status.HTTP_204_NO_CONTENT
            assert not CycleIssue.objects.filter(issue=issue, cycle=cycle).exists()
            assert GoogleCalendarEvent.objects.filter(entity_id=cycle.id).count() == 1
            assert len(callbacks) == 1
            _run_callbacks(callbacks)

        assert not GoogleCalendarEvent.objects.filter(entity_id=cycle.id).exists()
        provider_client.delete_event.assert_called_once()
        signature.assert_called_once_with(GOOGLE_CALENDAR_CYCLE_SYNC_TASK)
        targeted_cycle_task.set.assert_called_once_with(countdown=0)
        targeted_cycle_task.delay.assert_called_once_with(str(cycle.id), str(connection.id))
        fallback_cycle_dispatch.assert_not_called()
