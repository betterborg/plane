# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import datetime, timezone

import pytest


MIGRATE_FROM = ("db", "0129_project_google_calendar_sync")
MIGRATE_TO = ("db", "0130_snapshot_cycle_timezones")


@pytest.mark.migration
class TestCycleTimezoneSnapshotMigration:
    def test_snapshots_legacy_cycle_timezones_without_changing_instants(self, migrate_to):
        old_apps = migrate_to(MIGRATE_FROM)
        User = old_apps.get_model("db", "User")
        Workspace = old_apps.get_model("db", "Workspace")
        Project = old_apps.get_model("db", "Project")
        Cycle = old_apps.get_model("db", "Cycle")

        owner = User.objects.create(username="cycle-owner", email="cycle-owner@example.com")
        workspace = Workspace.objects.create(
            name="Cycle migration workspace",
            slug="cycle-migration-workspace",
            owner=owner,
        )

        explicit_project = Project.objects.create(
            name="Explicit timezone project",
            identifier="EXPLICIT",
            workspace=workspace,
            timezone="UTC",
        )
        utc_project = Project.objects.create(
            name="UTC project",
            identifier="UTC",
            workspace=workspace,
            timezone="UTC",
        )
        ambiguous_project = Project.objects.create(
            name="Ambiguous timezone project",
            identifier="AMBIGUOUS",
            workspace=workspace,
            timezone="UTC",
        )

        instants = {
            "explicit": (
                datetime(2025, 1, 10, 5, tzinfo=timezone.utc),
                datetime(2025, 1, 17, 5, tzinfo=timezone.utc),
            ),
            "utc": (
                datetime(2025, 2, 10, tzinfo=timezone.utc),
                datetime(2025, 2, 17, tzinfo=timezone.utc),
            ),
            "ambiguous": (
                datetime(2025, 3, 9, 18, 30, tzinfo=timezone.utc),
                datetime(2025, 3, 16, 18, 30, tzinfo=timezone.utc),
            ),
        }
        cycles = {
            "explicit": Cycle.objects.create(
                name="Explicit timezone cycle",
                project=explicit_project,
                workspace=workspace,
                owned_by=owner,
                start_date=instants["explicit"][0],
                end_date=instants["explicit"][1],
                timezone="America/New_York",
            ),
            "utc": Cycle.objects.create(
                name="UTC cycle",
                project=utc_project,
                workspace=workspace,
                owned_by=owner,
                start_date=instants["utc"][0],
                end_date=instants["utc"][1],
            ),
            "ambiguous": Cycle.objects.create(
                name="Ambiguous default-UTC cycle",
                project=ambiguous_project,
                workspace=workspace,
                owned_by=owner,
                start_date=instants["ambiguous"][0],
                end_date=instants["ambiguous"][1],
            ),
        }

        Project.objects.filter(pk=explicit_project.pk).update(timezone="Europe/Berlin")
        Project.objects.filter(pk=ambiguous_project.pk).update(timezone="Asia/Kolkata")

        new_apps = migrate_to(MIGRATE_TO)
        Cycle = new_apps.get_model("db", "Cycle")

        expected_timezones = {
            "explicit": "America/New_York",
            "utc": "UTC",
            "ambiguous": "Asia/Kolkata",
        }
        for scenario, legacy_cycle in cycles.items():
            migrated_cycle = Cycle.objects.get(pk=legacy_cycle.pk)
            assert migrated_cycle.timezone == expected_timezones[scenario]
            assert migrated_cycle.start_date == instants[scenario][0]
            assert migrated_cycle.end_date == instants[scenario][1]
