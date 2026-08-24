# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import patch

import pytest
from rest_framework import status

from plane.bgtasks.deletion_task import soft_delete_related_objects
from plane.bgtasks.google_calendar_task import resync_google_calendar_label
from plane.db.models import Issue, IssueLabel
from plane.db.signals import suppress_google_calendar_issue_signal_dispatch
from plane.tests.factories import IssueFactory, IssueLabelFactory, LabelFactory, ProjectFactory, ProjectMemberFactory


@pytest.fixture
def project(workspace, create_user):
    project = ProjectFactory(workspace=workspace)
    ProjectMemberFactory(project=project, member=create_user, role=20)
    return project


@pytest.fixture
def label_issue(project):
    label = LabelFactory(project=project)
    with suppress_google_calendar_issue_signal_dispatch():
        issue = IssueFactory(project=project)
        IssueLabelFactory(issue=issue, label=label, project=project)
    return label, issue


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarLabelDispatch:
    def _mutate_label_and_execute_commit(self, client, url, label, issue, method):
        callbacks = []
        recursive_deletions = []
        observed = []

        def capture_on_commit(callback, robust=False):
            callbacks.append((callback, robust))

        def inspect_saved_labels(issue_ids):
            for issue_id in issue_ids:
                saved_issue = Issue.all_objects.get(id=issue_id)
                active_labels = list(
                    IssueLabel.objects.filter(issue_id=issue_id, label__deleted_at__isnull=True).values_list(
                        "label__name", flat=True
                    )
                )
                observed.append((saved_issue.id, active_labels))

        with (
            patch(
                "plane.integrations.google_calendar.dispatch.transaction.on_commit",
                side_effect=capture_on_commit,
            ),
            patch.object(
                resync_google_calendar_label,
                "delay",
                side_effect=lambda *args, **kwargs: resync_google_calendar_label.run(*args, **kwargs),
            ),
            patch(
                "plane.db.mixins.soft_delete_related_objects.delay",
                side_effect=lambda *args, **kwargs: recursive_deletions.append((args, kwargs)),
            ),
            patch("plane.db.signals.dispatch_google_calendar_issue_syncs", side_effect=inspect_saved_labels),
        ):
            if method == "patch":
                response = client.patch(url, {"name": "Customer request"}, format="json")
                expected_status = status.HTTP_200_OK
            else:
                response = client.delete(url)
                expected_status = status.HTTP_204_NO_CONTENT

            assert response.status_code == expected_status, response.data
            assert observed == []
            assert len(callbacks) == 1

            if method == "delete":
                assert len(recursive_deletions) == 1
                with suppress_google_calendar_issue_signal_dispatch():
                    deletion_args, deletion_kwargs = recursive_deletions[0]
                    soft_delete_related_objects.run(*deletion_args, **deletion_kwargs)

            callback, robust = callbacks[0]
            assert robust is True
            callback()

        label.refresh_from_db()
        if method == "patch":
            assert label.name == "Customer request"
            assert label.deleted_at is None
            assert observed == [(issue.id, ["Customer request"])]
        else:
            assert label.deleted_at is not None
            assert not IssueLabel.objects.filter(issue_id=issue.id, label_id=label.id).exists()
            assert observed == [(issue.id, [])]

    @pytest.mark.parametrize("method", ("patch", "delete"))
    def test_app_mutation_fans_out_after_saved_label_commits(
        self,
        method,
        session_client,
        workspace,
        project,
        label_issue,
    ):
        label, issue = label_issue
        url = f"/api/workspaces/{workspace.slug}/projects/{project.id}/issue-labels/{label.id}/"

        self._mutate_label_and_execute_commit(session_client, url, label, issue, method)

    @pytest.mark.parametrize("method", ("patch", "delete"))
    def test_public_mutation_fans_out_after_saved_label_commits(
        self,
        method,
        api_key_client,
        workspace,
        project,
        label_issue,
    ):
        label, issue = label_issue
        url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/labels/{label.id}/"

        self._mutate_label_and_execute_commit(api_key_client, url, label, issue, method)
