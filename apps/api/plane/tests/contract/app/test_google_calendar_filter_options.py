# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from plane.db.models import Label, WorkspaceMember
from plane.tests.factories import ProjectFactory, WorkspaceFactory


def _filter_options_url(workspace):
    return reverse("google-calendar-filter-options", kwargs={"slug": workspace.slug})


@pytest.mark.contract
class TestGoogleCalendarFilterOptions:
    @pytest.mark.django_db
    def test_role_20_receives_active_workspace_labels_and_canonical_priorities(
        self,
        session_client,
        workspace,
    ):
        alpha_project = ProjectFactory(workspace=workspace, name="Alpha project")
        beta_project = ProjectFactory(workspace=workspace, name="Beta project")
        archived_project = ProjectFactory(
            workspace=workspace,
            name="Archived project",
            archived_at=timezone.now(),
        )
        alpha_label = Label.objects.create(
            workspace=workspace,
            project=alpha_project,
            name="Alpha label",
            color="#111111",
            description="private label description",
            external_source="private-provider",
            external_id="private-external-id",
        )
        beta_label = Label.objects.create(
            workspace=workspace,
            project=beta_project,
            name="Beta label",
            color="#222222",
        )
        workspace_label = Label.objects.create(
            workspace=workspace,
            name="Workspace label",
            color="#333333",
        )
        Label.objects.create(
            workspace=workspace,
            project=archived_project,
            name="Archived label",
            color="#444444",
        )
        other_workspace = WorkspaceFactory()
        Label.objects.create(
            workspace=other_workspace,
            project=ProjectFactory(workspace=other_workspace),
            name="Foreign label",
            color="#555555",
        )

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.get(_filter_options_url(workspace))

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {
            "labels": [
                {
                    "id": str(alpha_label.id),
                    "name": "Alpha label",
                    "color": "#111111",
                    "project_id": alpha_project.id,
                },
                {
                    "id": str(beta_label.id),
                    "name": "Beta label",
                    "color": "#222222",
                    "project_id": beta_project.id,
                },
                {
                    "id": str(workspace_label.id),
                    "name": "Workspace label",
                    "color": "#333333",
                    "project_id": None,
                },
            ],
            "priorities": [
                {"key": "urgent", "title": "Urgent"},
                {"key": "high", "title": "High"},
                {"key": "medium", "title": "Medium"},
                {"key": "low", "title": "Low"},
                {"key": "none", "title": "None"},
            ],
        }
        serialized = str(response.data)
        for private_value in (
            "private label description",
            "private-provider",
            "private-external-id",
            "Archived label",
            "Foreign label",
        ):
            assert private_value not in serialized

    @pytest.mark.django_db
    @pytest.mark.parametrize("membership", ("role_15", "role_5", "inactive", "outsider"))
    def test_only_active_role_20_can_read_filter_options(
        self,
        session_client,
        workspace,
        create_user,
        membership,
    ):
        if membership == "outsider":
            WorkspaceMember.objects.filter(workspace=workspace, member=create_user).delete()
        else:
            role = 15 if membership in {"role_15", "inactive"} else 5
            WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(
                role=role,
                is_active=membership != "inactive",
            )

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.get(_filter_options_url(workspace))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.django_db
    def test_unreleased_filter_options_are_not_found(self, session_client, workspace):
        with override_settings(GOOGLE_CALENDAR_RELEASED=False):
            response = session_client.get(_filter_options_url(workspace))

        assert response.status_code == status.HTTP_404_NOT_FOUND
