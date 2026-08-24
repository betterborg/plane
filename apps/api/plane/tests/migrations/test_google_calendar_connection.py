# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from django.db import IntegrityError, connection, transaction

from plane.db.models import GoogleCalendarConnection
from plane.tests.factories import (
    GoogleCalendarConnectionFactory,
    google_calendar_connection_scenario,
)


MIGRATE_FROM = ("db", "0122_alter_draftissue_assignees_alter_issue_assignees_and_more")
MIGRATE_TO = ("db", "0126_google_calendar_cleanup_intent")


@pytest.mark.migration
class TestGoogleCalendarConnectionMigration:
    def test_preserves_legacy_integrations_and_adds_connection_schema(self, migrate_to):
        old_apps = migrate_to(MIGRATE_FROM)
        User = old_apps.get_model("db", "User")
        Workspace = old_apps.get_model("db", "Workspace")
        APIToken = old_apps.get_model("db", "APIToken")
        Integration = old_apps.get_model("db", "Integration")
        WorkspaceIntegration = old_apps.get_model("db", "WorkspaceIntegration")

        Integration.objects.filter(provider="google_calendar").delete()
        assert not Integration.objects.filter(provider="google_calendar").exists()

        actor = User.objects.create(username="integration-bot", email="integration-bot@example.com", is_bot=True)
        member = User.objects.create(username="calendar-member", email="calendar-member@example.com")
        workspace = Workspace.objects.create(name="Migration workspace", slug="migration-workspace", owner=member)
        api_token = APIToken.objects.create(user=actor, workspace=workspace)

        legacy_ids = {}
        for provider in ("github", "slack"):
            integration = Integration.objects.create(title=provider.title(), provider=provider)
            workspace_integration = WorkspaceIntegration.objects.create(
                workspace=workspace,
                integration=integration,
                actor=actor,
                api_token=api_token,
            )
            legacy_ids[provider] = workspace_integration.id

        new_apps = migrate_to(MIGRATE_TO)
        Integration = new_apps.get_model("db", "Integration")
        WorkspaceIntegration = new_apps.get_model("db", "WorkspaceIntegration")
        CalendarConnection = new_apps.get_model("db", "GoogleCalendarConnection")

        calendar_integration = Integration.objects.get(provider="google_calendar")
        assert calendar_integration.title == "Google Calendar"
        assert calendar_integration.verified is True

        for provider, workspace_integration_id in legacy_ids.items():
            preserved = WorkspaceIntegration.objects.get(id=workspace_integration_id)
            assert preserved.integration.provider == provider
            assert preserved.actor_id == actor.id
            assert preserved.api_token_id == api_token.id

        calendar_workspace_integration = WorkspaceIntegration.objects.create(
            workspace_id=workspace.id,
            integration=calendar_integration,
            actor=None,
            api_token=None,
        )
        connection_row = CalendarConnection.objects.create(
            workspace_integration=calendar_workspace_integration,
            member_id=member.id,
            oauth_state="attempt-state",
            oauth_code_verifier="attempt-verifier",
            oauth_redirect_uri="https://plane.example/calendar/callback",
            calendar_id="plane-calendar",
            access_token="provider-access-token",
            refresh_token="provider-refresh-token",
        )

        assert connection_row.desired_state == "disconnected"
        assert connection_row.status == "disconnected"
        assert connection_row.lifecycle_generation == 0
        assert connection_row.oauth_state == "attempt-state"
        assert connection_row.calendar_id == "plane-calendar"
        assert connection_row.calendar_operation_id is None
        assert connection_row.retain_grant_after_cleanup is False

        table = CalendarConnection._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(
                f'SELECT "access_token", "refresh_token" FROM "{table}" WHERE "id" = %s', [connection_row.id]
            )
            stored_access_token, stored_refresh_token = cursor.fetchone()
        assert stored_access_token != "provider-access-token"
        assert stored_refresh_token != "provider-refresh-token"
        assert stored_access_token.startswith("fernet$")
        assert stored_refresh_token.startswith("fernet$")

        persisted_connection = CalendarConnection.objects.get(pk=connection_row.pk)
        assert persisted_connection.access_token == "provider-access-token"
        assert persisted_connection.refresh_token == "provider-refresh-token"

        with pytest.raises(IntegrityError), transaction.atomic():
            CalendarConnection.objects.create(
                workspace_integration=calendar_workspace_integration,
                member_id=member.id,
            )

    def test_factories_cover_lifecycle_states(self, migrate_to):
        migrate_to(MIGRATE_TO)

        assert google_calendar_connection_scenario() is None

        attempt = google_calendar_connection_scenario("attempt_only")
        assert attempt.oauth_state
        assert attempt.workspace_integration.integration.provider == "google_calendar"
        assert GoogleCalendarConnection.objects.filter(
            workspace_integration__integration__provider="google_calendar",
            pk=attempt.pk,
        ).exists()
        assert attempt.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        assert attempt.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert attempt.lifecycle_generation == 0
        assert not attempt.provider_account_id
        assert not attempt.provider_email
        assert not attempt.access_token
        assert not attempt.refresh_token
        assert attempt.token_expires_at is None
        assert attempt.scopes == []
        assert not attempt.last_error

        broken = google_calendar_connection_scenario("bound_broken")
        assert broken.provider_account_id
        assert broken.status == GoogleCalendarConnection.Status.ERROR
        assert broken.last_error

        tombstone = google_calendar_connection_scenario("tombstone")
        assert tombstone.lifecycle_generation == 1
        assert tombstone.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        assert tombstone.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert not tombstone.provider_account_id
        assert not tombstone.provider_email
        assert not tombstone.access_token
        assert not tombstone.refresh_token
        assert tombstone.token_expires_at is None
        assert tombstone.scopes == []
        assert not tombstone.last_error
        assert not tombstone.oauth_state
        assert not tombstone.oauth_code_verifier
        assert not tombstone.oauth_redirect_uri
        assert tombstone.oauth_attempt_expires_at is None

        pending_cleanup = GoogleCalendarConnectionFactory(pending_cleanup=True)
        assert pending_cleanup.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert pending_cleanup.lifecycle_generation == 2
        assert pending_cleanup.refresh_token == "encrypted-at-rest-refresh-token"
