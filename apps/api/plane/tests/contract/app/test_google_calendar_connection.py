# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import Mock, patch

import pytest
from django.test import override_settings
from django.urls import reverse
from rest_framework import status

from plane.bgtasks.google_calendar_task import reconcile_google_calendar_connection
from plane.db.models import GoogleCalendarConnection, WorkspaceMember
from plane.integrations.google_calendar.client import GoogleCalendarClientError
from plane.integrations.google_calendar.oauth import GoogleCalendarOAuthCredentials
from plane.tests.factories import (
    GoogleCalendarConnectionFactory,
    IntegrationFactory,
    UserFactory,
    WorkspaceIntegrationFactory,
)


@pytest.fixture
def calendar_workspace_integration(db, workspace):
    return WorkspaceIntegrationFactory(
        workspace=workspace,
        integration=IntegrationFactory(title="Google Calendar", provider="google_calendar"),
        config={"enabled": True},
    )


def _connection_url(workspace, member):
    return reverse(
        "google-calendar-connection",
        kwargs={"slug": workspace.slug, "member_id": member.id},
    )


def _active_connection(calendar_workspace_integration, member):
    return GoogleCalendarConnectionFactory(
        workspace_integration=calendar_workspace_integration,
        member=member,
        provider_account_id="google-account",
        provider_email="member@example.com",
        calendar_id="plane-calendar",
        refresh_token="refresh-token",
        oauth_state="outstanding-oauth-attempt",
        oauth_code_verifier="outstanding-code-verifier",
        desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
        status=GoogleCalendarConnection.Status.ACTIVE,
        lifecycle_generation=7,
    )


@pytest.mark.contract
class TestGoogleCalendarConnection:
    @pytest.mark.django_db
    def test_unreleased_disconnect_is_not_found(
        self,
        session_client,
        workspace,
        create_user,
        calendar_workspace_integration,
    ):
        connection = _active_connection(calendar_workspace_integration, create_user)

        with override_settings(GOOGLE_CALENDAR_RELEASED=False):
            response = session_client.delete(_connection_url(workspace, create_user))

        assert response.status_code == status.HTTP_404_NOT_FOUND
        connection.refresh_from_db()
        assert connection.status == GoogleCalendarConnection.Status.ACTIVE
        assert connection.lifecycle_generation == 7

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.parametrize("role", (20, 15, 5))
    def test_active_members_disconnect_their_own_exact_generation(
        self,
        session_client,
        workspace,
        create_user,
        calendar_workspace_integration,
        role,
    ):
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=role)
        connection = _active_connection(calendar_workspace_integration, create_user)
        lifecycle_task = Mock()

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.app.views.integration.current_app.signature", return_value=lifecycle_task) as signature,
        ):
            response = session_client.delete(_connection_url(workspace, create_user))

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.data == {"status": "disconnecting"}
        connection.refresh_from_db()
        assert connection.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        assert connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert connection.lifecycle_generation == 8
        assert connection.oauth_state == ""
        assert connection.oauth_code_verifier == ""
        assert connection.retain_grant_after_cleanup is False
        signature.assert_called_once_with("plane.bgtasks.google_calendar_task.reconcile_google_calendar_connection")
        lifecycle_task.delay.assert_called_once_with(str(connection.id), 8)

    @pytest.mark.django_db
    @pytest.mark.parametrize("role", (20, 15, 5))
    def test_active_members_cannot_disconnect_another_member(
        self,
        session_client,
        workspace,
        create_user,
        calendar_workspace_integration,
        role,
    ):
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=role)
        other_member = UserFactory()
        WorkspaceMember.objects.create(workspace=workspace, member=other_member, role=15)
        connection = _active_connection(calendar_workspace_integration, other_member)

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.delete(_connection_url(workspace, other_member))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        connection.refresh_from_db()
        assert connection.status == GoogleCalendarConnection.Status.ACTIVE
        assert connection.lifecycle_generation == 7
        assert connection.oauth_state == "outstanding-oauth-attempt"

    @pytest.mark.django_db
    @pytest.mark.parametrize("membership", ("inactive", "outsider"))
    def test_inactive_members_and_outsiders_cannot_disconnect(
        self,
        session_client,
        workspace,
        create_user,
        calendar_workspace_integration,
        membership,
    ):
        connection = _active_connection(calendar_workspace_integration, create_user)
        if membership == "inactive":
            WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(is_active=False)
        else:
            WorkspaceMember.objects.filter(workspace=workspace, member=create_user).delete()

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.delete(_connection_url(workspace, create_user))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        connection.refresh_from_db()
        assert connection.status == GoogleCalendarConnection.Status.ACTIVE
        assert connection.lifecycle_generation == 7

    @pytest.mark.django_db(transaction=True)
    def test_broker_failure_keeps_accepted_generation_recoverable(
        self,
        session_client,
        workspace,
        create_user,
        calendar_workspace_integration,
    ):
        connection = _active_connection(calendar_workspace_integration, create_user)
        lifecycle_task = Mock()
        lifecycle_task.delay.side_effect = RuntimeError("broker unavailable")

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.app.views.integration.current_app.signature", return_value=lifecycle_task),
        ):
            response = session_client.delete(_connection_url(workspace, create_user))

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.data == {"status": "disconnecting"}
        connection.refresh_from_db()
        assert connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert connection.lifecycle_generation == 8
        lifecycle_task.delay.assert_called_once_with(str(connection.id), 8)

        provider_client = Mock(access_token=None)
        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=provider_client),
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 8)

        connection.refresh_from_db()
        assert result == "disconnected"
        provider_client.delete_calendar.assert_called_once_with("plane-calendar")
        provider_client.revoke_grant.assert_called_once_with()
        assert connection.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert connection.lifecycle_generation == 8
        assert connection.provider_account_id == ""
        assert connection.refresh_token == ""

    @pytest.mark.django_db(transaction=True)
    def test_failed_cleanup_remains_nonterminal_and_blocks_reconnection(
        self,
        session_client,
        workspace,
        create_user,
        calendar_workspace_integration,
    ):
        connection = _active_connection(calendar_workspace_integration, create_user)
        lifecycle_task = Mock()

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.app.views.integration.current_app.signature", return_value=lifecycle_task),
        ):
            response = session_client.delete(_connection_url(workspace, create_user))

        assert response.status_code == status.HTTP_202_ACCEPTED
        provider_client = Mock(access_token=None)
        provider_client.delete_calendar.side_effect = GoogleCalendarClientError("Google Calendar deletion failed")
        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=provider_client),
        ):
            result = reconcile_google_calendar_connection(str(connection.id), 8)

        connection.refresh_from_db()
        assert result == "error"
        assert connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert connection.lifecycle_generation == 8
        assert connection.provider_account_id == "google-account"
        assert connection.calendar_id == "plane-calendar"

        oauth_credentials = GoogleCalendarOAuthCredentials(
            client_id="calendar-client",
            client_secret="calendar-secret",
        )
        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.app.views.google_calendar_oauth.get_google_calendar_oauth_credentials",
                return_value=oauth_credentials,
            ),
        ):
            reconnect_response = session_client.get(
                reverse("google-calendar-oauth-start", kwargs={"slug": workspace.slug})
            )

        assert reconnect_response.status_code == status.HTTP_409_CONFLICT
        assert reconnect_response.data == {"error": "google_calendar_disconnect_in_progress"}
