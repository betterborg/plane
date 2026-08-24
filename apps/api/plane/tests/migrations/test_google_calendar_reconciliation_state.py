# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest


MIGRATE_FROM = ("db", "0130_snapshot_cycle_timezones")
MIGRATE_TO = ("db", "0131_google_calendar_reconciliation_state")


@pytest.mark.migration
class TestGoogleCalendarReconciliationStateMigration:
    def test_existing_rows_receive_safe_empty_reconciliation_defaults(self, migrate_to):
        old_apps = migrate_to(MIGRATE_FROM)
        User = old_apps.get_model("db", "User")
        Workspace = old_apps.get_model("db", "Workspace")
        Integration = old_apps.get_model("db", "Integration")
        WorkspaceIntegration = old_apps.get_model("db", "WorkspaceIntegration")
        CalendarConnection = old_apps.get_model("db", "GoogleCalendarConnection")
        CalendarEvent = old_apps.get_model("db", "GoogleCalendarEvent")

        member = User.objects.create(username="reconciliation-member", email="reconciliation@example.com")
        workspace = Workspace.objects.create(name="Reconciliation workspace", slug="reconciliation", owner=member)
        integration, _ = Integration.objects.get_or_create(
            provider="google_calendar",
            defaults={"title": "Google Calendar"},
        )
        workspace_integration = WorkspaceIntegration.objects.create(
            workspace=workspace,
            integration=integration,
            actor=None,
            api_token=None,
        )
        calendar_connection = CalendarConnection.objects.create(
            workspace_integration=workspace_integration,
            member=member,
            calendar_id="existing-calendar",
        )
        calendar_event = CalendarEvent.objects.create(
            connection=calendar_connection,
            entity_type="work_item",
            entity_id=workspace.id,
            google_event_id="existing-event",
            payload_hash="0" * 64,
        )

        new_apps = migrate_to(MIGRATE_TO)
        CalendarConnection = new_apps.get_model("db", "GoogleCalendarConnection")
        CalendarEvent = new_apps.get_model("db", "GoogleCalendarEvent")
        migrated_connection = CalendarConnection.objects.get(pk=calendar_connection.pk)
        migrated_event = CalendarEvent.objects.get(pk=calendar_event.pk)

        assert migrated_connection.sync_token == ""
        assert migrated_connection.page_token == ""
        assert migrated_connection.credential_fingerprint == ""
        assert migrated_connection.reconciliation_lease_expires_at is None
        assert migrated_connection.reconciliation_completed_at is None
        assert migrated_connection.reconciliation_phase == ""
        assert migrated_connection.reconciliation_cursor == ""
        assert migrated_connection.calendar_generation == 0
        assert migrated_event.calendar_generation == 0
        assert migrated_event.provider_etag == ""
        assert migrated_event.provider_payload_hash == ""
        assert migrated_event.provider_status == ""
