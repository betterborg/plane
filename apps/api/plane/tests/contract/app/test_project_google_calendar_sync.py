# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from rest_framework import status

from plane.db.models import Project, ProjectMember


@pytest.mark.contract
class TestProjectGoogleCalendarSyncField:
    @pytest.mark.django_db
    def test_project_patch_exposes_but_does_not_mutate_calendar_sync(self, session_client, workspace, create_user):
        project = Project.objects.create(name="Calendar project", identifier="CAL", workspace=workspace)
        ProjectMember.objects.create(project=project, member=create_user, role=20, is_active=True)

        response = session_client.patch(
            f"/api/workspaces/{workspace.slug}/projects/{project.id}/",
            {"google_calendar_sync_enabled": False},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["google_calendar_sync_enabled"] is True
        project.refresh_from_db()
        assert project.google_calendar_sync_enabled is True
