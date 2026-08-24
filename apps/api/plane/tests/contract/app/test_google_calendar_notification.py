# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import patch

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from plane.bgtasks.google_calendar_task import _mark_authorization_failure, send_google_calendar_disconnected_email
from plane.db.models import WorkspaceMember
from plane.integrations.google_calendar.client import GoogleCalendarInvalidGrant
from plane.tests.factories import GoogleCalendarConnectionFactory, UserFactory


def _notifications_url(workspace):
    return reverse("notifications", kwargs={"slug": workspace.slug})


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarNotification:
    def test_broken_connection_notification_is_exposed_to_its_member(self, session_client, workspace, create_user):
        connection = GoogleCalendarConnectionFactory(
            active=True,
            workspace_integration__workspace=workspace,
            member=create_user,
            provider_account_id="google-account-id",
            provider_email="member@example.com",
            access_token="provider-access-token",
            refresh_token="provider-refresh-token",
        )

        with patch.object(send_google_calendar_disconnected_email, "delay"):
            _mark_authorization_failure(connection, GoogleCalendarInvalidGrant("refresh rejected"))

        response = session_client.get(_notifications_url(workspace))

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        notification = response.data[0]
        assert notification["entity_name"] == "google_calendar_connection"
        assert notification["entity_identifier"] == str(connection.id)
        assert notification["project"] is None
        assert notification["data"] == {
            "google_calendar_connection": {
                "id": str(connection.id),
                "provider_email": "member@example.com",
                "status": "broken",
                "error": "refresh_token_invalid",
                "action_url": f"/{workspace.slug}/settings/integrations/google-calendar",
            }
        }
        assert "is_inbox_issue" not in notification
        assert "is_intake_issue" not in notification
        assert "is_mentioned_notification" not in notification
        serialized_notification = str(notification)
        assert "access_token" not in serialized_notification
        assert "refresh_token" not in serialized_notification
        assert "provider-access-token" not in serialized_notification
        assert "provider-refresh-token" not in serialized_notification
        assert "provider_account_id" not in serialized_notification

    def test_calendar_notification_is_not_exposed_to_other_members(self, workspace, create_user):
        connection = GoogleCalendarConnectionFactory(
            active=True,
            workspace_integration__workspace=workspace,
            member=create_user,
        )
        other_member = UserFactory()
        WorkspaceMember.objects.create(workspace=workspace, member=other_member, role=15)

        with patch.object(send_google_calendar_disconnected_email, "delay"):
            _mark_authorization_failure(connection, GoogleCalendarInvalidGrant("refresh rejected"))

        other_member_client = APIClient()
        other_member_client.force_authenticate(user=other_member)
        response = other_member_client.get(_notifications_url(workspace))

        assert response.status_code == status.HTTP_200_OK
        assert response.data == []

        outsider = UserFactory()
        outsider_client = APIClient()
        outsider_client.force_authenticate(user=outsider)
        response = outsider_client.get(_notifications_url(workspace))

        assert response.status_code == status.HTTP_403_FORBIDDEN
