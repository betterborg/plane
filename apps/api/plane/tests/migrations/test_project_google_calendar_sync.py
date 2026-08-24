# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest


MIGRATE_FROM = ("db", "0128_google_calendar_events")
MIGRATE_TO = ("db", "0129_project_google_calendar_sync")


@pytest.mark.migration
class TestProjectGoogleCalendarSyncMigration:
    def test_defaults_existing_and_new_projects_to_calendar_sync(self, migrate_to):
        old_apps = migrate_to(MIGRATE_FROM)
        User = old_apps.get_model("db", "User")
        Workspace = old_apps.get_model("db", "Workspace")
        Project = old_apps.get_model("db", "Project")

        owner = User.objects.create(username="calendar-owner", email="calendar-owner@example.com")
        workspace = Workspace.objects.create(name="Calendar workspace", slug="calendar-workspace", owner=owner)
        existing_project = Project.objects.create(name="Existing project", identifier="EXIST", workspace=workspace)

        new_apps = migrate_to(MIGRATE_TO)
        Project = new_apps.get_model("db", "Project")

        migrated_project = Project.objects.get(pk=existing_project.pk)
        assert migrated_project.google_calendar_sync_enabled is True

        new_project = Project.objects.create(name="New project", identifier="NEW", workspace_id=workspace.pk)
        assert new_project.google_calendar_sync_enabled is True
