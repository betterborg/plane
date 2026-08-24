# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import patch

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from plane.db.models import Project
from plane.integrations.google_calendar.dispatch import GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK
from plane.tests.factories import ProjectFactory, ProjectMemberFactory


def _project_sync_url(workspace, project):
    return reverse(
        "google-calendar-project-sync",
        kwargs={"slug": workspace.slug, "project_id": project.id},
    )


def _project_archive_url(workspace, project):
    return reverse(
        "project-archive-unarchive",
        kwargs={"slug": workspace.slug, "project_id": project.id},
    )


def _record_committed_project_state(observed_states):
    def record(task, project_id):
        project = Project.objects.get(id=project_id)
        observed_states.append(
            {
                "task": task.task,
                "project_id": str(project.id),
                "included": project.google_calendar_sync_enabled,
                "archived": project.archived_at is not None,
            }
        )

    return record


def _mutate_project(session_client, workspace, project, mutation):
    if mutation == "inclusion":
        return session_client.patch(
            _project_sync_url(workspace, project),
            {"google_calendar_sync_enabled": False},
            format="json",
        )
    if mutation == "archive":
        return session_client.post(_project_archive_url(workspace, project))
    if mutation == "restore":
        return session_client.delete(_project_archive_url(workspace, project))
    raise AssertionError(f"Unsupported project mutation: {mutation}")


@pytest.mark.contract
class TestGoogleCalendarProjectSync:
    @pytest.mark.django_db(transaction=True)
    def test_role_20_mutations_publish_committed_project_state_once(
        self,
        session_client,
        workspace,
        create_user,
    ):
        project = ProjectFactory(workspace=workspace)
        ProjectMemberFactory(project=project, member=create_user, role=20)
        observed_states = []

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch(
                "plane.integrations.google_calendar.dispatch.publish_google_calendar_task",
                side_effect=_record_committed_project_state(observed_states),
            ) as publish,
        ):
            get_response = session_client.get(_project_sync_url(workspace, project))
            assert get_response.status_code == status.HTTP_200_OK
            assert get_response.data == {"google_calendar_sync_enabled": True}
            publish.assert_not_called()

            inclusion_response = _mutate_project(session_client, workspace, project, "inclusion")
            assert inclusion_response.status_code == status.HTTP_200_OK
            assert inclusion_response.data == {"google_calendar_sync_enabled": False}
            assert publish.call_count == 1

            archive_response = _mutate_project(session_client, workspace, project, "archive")
            assert archive_response.status_code == status.HTTP_200_OK
            assert publish.call_count == 2

            restore_response = _mutate_project(session_client, workspace, project, "restore")
            assert restore_response.status_code == status.HTTP_204_NO_CONTENT
            assert publish.call_count == 3

        assert observed_states == [
            {
                "task": GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK,
                "project_id": str(project.id),
                "included": False,
                "archived": False,
            },
            {
                "task": GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK,
                "project_id": str(project.id),
                "included": False,
                "archived": True,
            },
            {
                "task": GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK,
                "project_id": str(project.id),
                "included": False,
                "archived": False,
            },
        ]

    @pytest.mark.django_db(transaction=True)
    def test_role_15_cannot_read_or_mutate_project_inclusion(
        self,
        session_client,
        workspace,
        create_user,
    ):
        project = ProjectFactory(workspace=workspace)
        ProjectMemberFactory(project=project, member=create_user, role=15)

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.integrations.google_calendar.dispatch.publish_google_calendar_task") as publish,
        ):
            get_response = session_client.get(_project_sync_url(workspace, project))
            patch_response = session_client.patch(
                _project_sync_url(workspace, project),
                {"google_calendar_sync_enabled": False},
                format="json",
            )

        assert get_response.status_code == status.HTTP_403_FORBIDDEN
        assert patch_response.status_code == status.HTTP_403_FORBIDDEN
        publish.assert_not_called()
        project.refresh_from_db()
        assert project.google_calendar_sync_enabled is True

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.parametrize(
        ("mutation", "expected_status", "expected_archived"),
        (
            ("inclusion", status.HTTP_200_OK, False),
            ("archive", status.HTTP_200_OK, True),
            ("restore", status.HTTP_204_NO_CONTENT, False),
        ),
    )
    def test_publication_failure_leaves_project_mutation_committed_and_retryable(
        self,
        session_client,
        workspace,
        create_user,
        mutation,
        expected_status,
        expected_archived,
    ):
        project = ProjectFactory(
            workspace=workspace,
            archived_at=timezone.now() if mutation == "restore" else None,
        )
        ProjectMemberFactory(project=project, member=create_user, role=20)

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch(
                "plane.integrations.google_calendar.dispatch.publish_google_calendar_task",
                side_effect=RuntimeError("broker unavailable"),
            ) as failed_publish,
        ):
            response = _mutate_project(session_client, workspace, project, mutation)

        assert response.status_code == expected_status
        failed_publish.assert_called_once()
        project.refresh_from_db()
        assert project.google_calendar_sync_enabled is (mutation != "inclusion")
        assert (project.archived_at is not None) is expected_archived

        recovered_states = []
        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch(
                "plane.integrations.google_calendar.dispatch.publish_google_calendar_task",
                side_effect=_record_committed_project_state(recovered_states),
            ) as recovered_publish,
        ):
            retry_response = _mutate_project(session_client, workspace, project, mutation)

        assert retry_response.status_code == expected_status
        recovered_publish.assert_called_once()
        assert recovered_states == [
            {
                "task": GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK,
                "project_id": str(project.id),
                "included": mutation != "inclusion",
                "archived": expected_archived,
            }
        ]
