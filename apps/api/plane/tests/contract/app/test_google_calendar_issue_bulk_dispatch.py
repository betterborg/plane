# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from plane.bgtasks.google_calendar_task import synchronize_google_calendar_cycle
from plane.db.models import CycleIssue, GoogleCalendarEvent, Issue, Module, ModuleIssue
from plane.db.signals import suppress_google_calendar_issue_signal_dispatch
from plane.tests.factories import (
    CycleFactory,
    CycleIssueFactory,
    GoogleCalendarConnectionFactory,
    IssueAssigneeFactory,
    IssueFactory,
    ProjectFactory,
    ProjectMemberFactory,
    StateFactory,
    WorkspaceIntegrationFactory,
)


@pytest.fixture
def project(workspace, create_user):
    project = ProjectFactory(workspace=workspace)
    ProjectMemberFactory(project=project, member=create_user)
    return project


def _issues(project, state_group="unstarted"):
    state = StateFactory(project=project, group=state_group)
    with suppress_google_calendar_issue_signal_dispatch():
        return [IssueFactory(project=project, state=state) for _ in range(2)]


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarIssueBulkDispatch:
    def test_bulk_dates_dispatch_after_persistence_and_ignore_broker_failure(
        self,
        session_client,
        workspace,
        project,
    ):
        issues = _issues(project)
        start_date = timezone.localdate() + timedelta(days=1)
        target_date = timezone.localdate() + timedelta(days=4)
        observed_issue_ids = []

        def inspect_state_then_fail(issue_id):
            issue = Issue.all_objects.get(id=issue_id)
            assert issue.start_date == start_date
            assert issue.target_date == target_date
            observed_issue_ids.append(issue.id)
            raise RuntimeError("broker unavailable")

        with (
            patch("plane.app.views.issue.base.issue_activity.delay"),
            patch(
                "plane.db.signals.synchronize_google_calendar_issue.delay",
                side_effect=inspect_state_then_fail,
            ),
        ):
            response = session_client.post(
                reverse(
                    "project-issue-dates",
                    kwargs={"slug": workspace.slug, "project_id": project.id},
                ),
                {
                    "updates": [
                        {
                            "id": str(issue.id),
                            "start_date": start_date.isoformat(),
                            "target_date": target_date.isoformat(),
                        }
                        for issue in issues
                    ]
                },
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK
        assert set(observed_issue_ids) == {issue.id for issue in issues}
        for issue in issues:
            issue.refresh_from_db()
            assert issue.start_date == start_date
            assert issue.target_date == target_date

    def test_bulk_archive_dispatch_after_persistence_and_ignore_broker_failure(
        self,
        session_client,
        workspace,
        project,
    ):
        issues = _issues(project, state_group="completed")
        observed_issue_ids = []

        def inspect_state_then_fail(issue_id):
            issue = Issue.all_objects.get(id=issue_id)
            assert issue.archived_at == timezone.localdate()
            observed_issue_ids.append(issue.id)
            raise RuntimeError("broker unavailable")

        with (
            patch("plane.app.views.issue.archive.issue_activity.delay"),
            patch(
                "plane.db.signals.synchronize_google_calendar_issue.delay",
                side_effect=inspect_state_then_fail,
            ),
        ):
            response = session_client.post(
                reverse(
                    "bulk-archive-issues",
                    kwargs={"slug": workspace.slug, "project_id": project.id},
                ),
                {"issue_ids": [str(issue.id) for issue in issues]},
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK
        assert set(observed_issue_ids) == {issue.id for issue in issues}
        for issue in issues:
            issue.refresh_from_db()
            assert issue.archived_at == timezone.localdate()

    def test_bulk_delete_captures_ids_before_soft_delete_and_ignore_broker_failure(
        self,
        session_client,
        workspace,
        project,
    ):
        issues = _issues(project)
        observed_issue_ids = []

        def inspect_state_then_fail(issue_id):
            issue = Issue.all_objects.get(id=issue_id)
            assert issue.deleted_at is not None
            observed_issue_ids.append(issue.id)
            raise RuntimeError("broker unavailable")

        with patch(
            "plane.db.signals.synchronize_google_calendar_issue.delay",
            side_effect=inspect_state_then_fail,
        ):
            response = session_client.delete(
                reverse(
                    "project-issues-bulk",
                    kwargs={"slug": workspace.slug, "project_id": project.id},
                ),
                {"issue_ids": [str(issue.id) for issue in issues]},
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK
        assert set(observed_issue_ids) == {issue.id for issue in issues}
        for issue in issues:
            issue.refresh_from_db()
            assert issue.deleted_at is not None

    def test_bulk_delete_dispatches_distinct_old_cycle_after_all_deletions(
        self,
        session_client,
        workspace,
        project,
    ):
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration__provider="google_calendar",
            config={"enabled": True, "recipients": "cycle_members"},
        )
        issues = _issues(project)
        cycle = CycleFactory(project=project)
        module = Module.objects.create(name="Bulk delete module", project=project)
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            active=True,
        )
        with suppress_google_calendar_issue_signal_dispatch():
            for issue in issues:
                CycleIssueFactory(cycle=cycle, issue=issue, project=project)
                ModuleIssue.objects.create(module=module, issue=issue, project=project)
            IssueAssigneeFactory(issue=issues[0], assignee=connection.member, project=project)

        provider_client = Mock()
        provider_client.access_token = None
        provider_client.list_events.return_value = []
        observed_issue_ids = []
        observed_cycle_ids = []

        def inspect_deleted_issue(issue_id):
            issue = Issue.all_objects.get(id=issue_id)
            assert issue.deleted_at is not None
            observed_issue_ids.append(issue.id)

        def converge_cycle_from_final_state(cycle_id):
            assert not CycleIssue.objects.filter(cycle_id=cycle_id).exists()
            assert not ModuleIssue.objects.filter(issue_id__in=[issue.id for issue in issues]).exists()
            assert not Issue.issue_objects.filter(id__in=[issue.id for issue in issues]).exists()
            observed_cycle_ids.append(cycle_id)
            return synchronize_google_calendar_cycle.run(cycle_id)

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=provider_client),
            patch("plane.db.signals.synchronize_google_calendar_issue.delay", side_effect=inspect_deleted_issue),
            patch.object(
                synchronize_google_calendar_cycle,
                "delay",
                side_effect=converge_cycle_from_final_state,
            ),
        ):
            assert synchronize_google_calendar_cycle.run(str(cycle.id)) == ["created"]
            assert GoogleCalendarEvent.objects.filter(entity_id=cycle.id).count() == 1

            response = session_client.delete(
                reverse(
                    "project-issues-bulk",
                    kwargs={"slug": workspace.slug, "project_id": project.id},
                ),
                {"issue_ids": [str(issue.id) for issue in issues]},
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK
        assert set(observed_issue_ids) == {issue.id for issue in issues}
        assert observed_cycle_ids == [str(cycle.id)]
        assert not CycleIssue.objects.filter(cycle=cycle).exists()
        assert not GoogleCalendarEvent.objects.filter(entity_id=cycle.id).exists()
        provider_client.delete_event.assert_called_once()
