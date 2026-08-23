# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from plane.db.models import Issue
from plane.db.signals import suppress_google_calendar_issue_signal_dispatch
from plane.tests.factories import IssueFactory, ProjectFactory, ProjectMemberFactory, StateFactory


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
