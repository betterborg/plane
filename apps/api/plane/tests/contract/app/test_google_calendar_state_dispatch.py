# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import patch

import pytest
from rest_framework import status

from plane.bgtasks.google_calendar_task import resync_google_calendar_state_issues
from plane.db.models import Issue
from plane.db.signals import suppress_google_calendar_issue_signal_dispatch
from plane.tests.factories import IssueFactory, ProjectFactory, ProjectMemberFactory, StateFactory


@pytest.fixture
def project(workspace, create_user):
    project = ProjectFactory(workspace=workspace)
    ProjectMemberFactory(project=project, member=create_user)
    return project


@pytest.fixture
def state_issue(project):
    state = StateFactory(project=project)
    with suppress_google_calendar_issue_signal_dispatch():
        issue = IssueFactory(project=project, state=state)
    return state, issue


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarStateDispatch:
    def _patch_state_and_execute_commit(self, client, url, state, issue):
        callbacks = []
        observed = []

        def capture_on_commit(callback, using=None, robust=False):
            callbacks.append((callback, robust))

        def inspect_saved_states(issue_ids):
            for issue_id in issue_ids:
                saved_issue = Issue.all_objects.select_related("state").get(id=issue_id)
                observed.append((saved_issue.id, saved_issue.state.name, saved_issue.state.group))

        with (
            patch(
                "plane.integrations.google_calendar.dispatch.transaction.on_commit",
                side_effect=capture_on_commit,
            ),
            patch.object(
                resync_google_calendar_state_issues,
                "delay",
                side_effect=lambda *args, **kwargs: resync_google_calendar_state_issues.run(*args, **kwargs),
            ),
            patch("plane.db.signals.dispatch_google_calendar_issue_syncs", side_effect=inspect_saved_states),
        ):
            response = client.patch(
                url,
                {"name": "Ready to ship", "group": "completed"},
                format="json",
            )

            assert response.status_code == status.HTTP_200_OK, response.data
            assert observed == []
            assert len(callbacks) == 1
            callback, robust = callbacks[0]
            assert robust is True
            callback()

        state.refresh_from_db()
        assert state.name == "Ready to ship"
        assert state.group == "completed"
        assert observed == [(issue.id, "Ready to ship", "completed")]

    def test_app_patch_fans_out_after_saved_state_commits(
        self,
        session_client,
        workspace,
        project,
        state_issue,
    ):
        state, issue = state_issue
        url = f"/api/workspaces/{workspace.slug}/projects/{project.id}/states/{state.id}/"

        self._patch_state_and_execute_commit(session_client, url, state, issue)

    def test_public_patch_fans_out_after_saved_state_commits(
        self,
        api_key_client,
        workspace,
        project,
        state_issue,
    ):
        state, issue = state_issue
        url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/states/{state.id}/"

        self._patch_state_and_execute_commit(api_key_client, url, state, issue)
