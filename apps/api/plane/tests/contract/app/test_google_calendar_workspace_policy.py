# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import Mock, patch

import pytest
from django.test import override_settings
from django.urls import reverse
from rest_framework import status

from plane.db.models import GoogleCalendarConnection, WorkspaceIntegration, WorkspaceMember
from plane.tests.factories import (
    GoogleCalendarConnectionFactory,
    IntegrationFactory,
    WorkspaceIntegrationFactory,
)


@pytest.fixture
def calendar_integration(db):
    return IntegrationFactory(title="Google Calendar", provider="google_calendar")


def _policy_url(workspace):
    return reverse("google-calendar-workspace-policy", kwargs={"slug": workspace.slug})


@pytest.mark.contract
class TestGoogleCalendarWorkspacePolicy:
    @pytest.mark.django_db
    def test_unreleased_policy_is_not_found(self, session_client, workspace, calendar_integration):
        with override_settings(GOOGLE_CALENDAR_RELEASED=False):
            response = session_client.patch(_policy_url(workspace), {"enabled": False}, format="json")

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert not WorkspaceIntegration.objects.filter(
            workspace=workspace,
            integration=calendar_integration,
        ).exists()

    @pytest.mark.django_db
    def test_only_role_20_can_patch_policy(self, session_client, workspace, create_user, calendar_integration):
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=15)

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.patch(_policy_url(workspace), {"enabled": False}, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not WorkspaceIntegration.objects.filter(
            workspace=workspace,
            integration=calendar_integration,
        ).exists()

    @pytest.mark.django_db
    def test_enable_rejects_incomplete_instance_credentials(
        self,
        session_client,
        workspace,
        calendar_integration,
    ):
        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.patch(_policy_url(workspace), {"enabled": True}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data == {"error": "google_calendar_credentials_incomplete"}
        assert not WorkspaceIntegration.objects.filter(
            workspace=workspace,
            integration=calendar_integration,
        ).exists()

    @pytest.mark.django_db
    def test_filter_policy_requires_label_or_priority(
        self,
        session_client,
        workspace,
        calendar_integration,
    ):
        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.patch(
                _policy_url(workspace),
                {"enabled": False, "mode": "filter"},
                format="json",
            )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "non_field_errors" in response.data

    @pytest.mark.django_db
    def test_reenable_conflict_preserves_policy_and_generation(
        self,
        session_client,
        workspace,
        calendar_integration,
    ):
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration=calendar_integration,
            config={
                "enabled": False,
                "mode": "assignment",
                "update_on_completion": True,
                "recipients": "cycle_members",
                "label_id": None,
                "priority": None,
            },
        )
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="google-account",
            calendar_id="calendar-awaiting-delete",
            refresh_token="validated-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=4,
        )

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.app.views.integration._has_complete_google_calendar_credentials", return_value=True),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
        ):
            response = session_client.patch(_policy_url(workspace), {"enabled": True}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data == {"error": "google_calendar_disable_cleanup_in_progress"}
        workspace_integration.refresh_from_db()
        connection.refresh_from_db()
        assert workspace_integration.config["enabled"] is False
        assert connection.lifecycle_generation == 4
        assert connection.calendar_id == "calendar-awaiting-delete"

    @pytest.mark.django_db(transaction=True)
    def test_broker_failure_keeps_committed_policy_and_exact_generation(
        self,
        session_client,
        workspace,
        calendar_integration,
    ):
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration=calendar_integration,
            config={"enabled": False},
        )
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="google-account",
            refresh_token="validated-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.DISCONNECTED,
            lifecycle_generation=4,
        )
        lifecycle_task = Mock()
        lifecycle_task.delay.side_effect = RuntimeError("broker unavailable")

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.app.views.integration._has_complete_google_calendar_credentials", return_value=True),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.app.views.integration.current_app.signature", return_value=lifecycle_task),
        ):
            response = session_client.patch(_policy_url(workspace), {"enabled": True}, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {
            "enabled": True,
            "mode": "assignment",
            "update_on_completion": True,
            "recipients": "cycle_members",
            "label_id": None,
            "priority": None,
        }
        workspace_integration.refresh_from_db()
        connection.refresh_from_db()
        assert workspace_integration.config == response.data
        assert connection.desired_state == GoogleCalendarConnection.DesiredState.CONNECTED
        assert connection.status == GoogleCalendarConnection.Status.PENDING
        assert connection.lifecycle_generation == 5
        lifecycle_task.delay.assert_called_once_with(str(connection.id), 5)
