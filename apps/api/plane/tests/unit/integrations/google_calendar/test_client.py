# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import UUID

import pytest
import requests
from django.utils import timezone

from plane.integrations.google_calendar.client import GoogleCalendarClient, GoogleCalendarClientError
from plane.integrations.google_calendar.oauth import GoogleCalendarOAuthCredentials


@pytest.mark.unit
class TestGoogleCalendarClient:
    def test_create_uses_the_dedicated_calendar_endpoint(self):
        response = Mock(status_code=200)
        response.json.return_value = {"id": "plane-calendar@example.com"}
        client = GoogleCalendarClient(
            access_token="access-token",
            refresh_token="refresh-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with patch("plane.integrations.google_calendar.client.requests.request", return_value=response) as request:
            calendar_id = client.create_calendar()

        assert calendar_id == "plane-calendar@example.com"
        request.assert_called_once_with(
            "post",
            "https://www.googleapis.com/calendar/v3/calendars",
            headers={"Authorization": "Bearer access-token"},
            timeout=10,
            json={"summary": "Plane"},
        )

    def test_create_records_the_durable_operation_marker(self):
        response = Mock(status_code=200)
        response.json.return_value = {"id": "plane-calendar@example.com"}
        client = GoogleCalendarClient(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )
        operation_id = UUID("12345678-1234-5678-1234-567812345678")

        with patch("plane.integrations.google_calendar.client.requests.request", return_value=response) as request:
            client.create_calendar(operation_id)

        assert request.call_args.kwargs["json"] == {
            "summary": "Plane",
            "description": "Plane calendar operation: 12345678-1234-5678-1234-567812345678",
        }

    def test_find_calendar_paginates_to_recover_a_completed_creation(self):
        first_response = Mock(status_code=200)
        first_response.json.return_value = {"items": [], "nextPageToken": "next-page"}
        second_response = Mock(status_code=200)
        second_response.json.return_value = {
            "items": [
                {
                    "id": "recovered-calendar@example.com",
                    "description": "Plane calendar operation: 12345678-1234-5678-1234-567812345678",
                }
            ]
        }
        client = GoogleCalendarClient(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with patch(
            "plane.integrations.google_calendar.client.requests.request",
            side_effect=[first_response, second_response],
        ) as request:
            calendar_id = client.find_calendar(UUID("12345678-1234-5678-1234-567812345678"))

        assert calendar_id == "recovered-calendar@example.com"
        assert request.call_count == 2
        assert request.call_args_list[0].kwargs["params"] == {"maxResults": 250, "minAccessRole": "owner"}
        assert request.call_args_list[1].kwargs["params"] == {
            "maxResults": 250,
            "minAccessRole": "owner",
            "pageToken": "next-page",
        }

    def test_delete_targets_only_the_recorded_encoded_calendar_id(self):
        response = Mock(status_code=204)
        client = GoogleCalendarClient(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with patch("plane.integrations.google_calendar.client.requests.request", return_value=response) as request:
            client.delete_calendar("plane/calendar@example.com")

        request.assert_called_once_with(
            "delete",
            "https://www.googleapis.com/calendar/v3/calendars/plane%2Fcalendar%40example.com",
            headers={"Authorization": "Bearer access-token"},
            timeout=10,
        )

    def test_delete_treats_an_already_absent_calendar_as_converged(self):
        response = Mock(status_code=404)
        client = GoogleCalendarClient(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with patch("plane.integrations.google_calendar.client.requests.request", return_value=response):
            client.delete_calendar("recorded-calendar")

        response.raise_for_status.assert_not_called()

    def test_revoke_treats_only_invalid_token_as_already_converged(self):
        response = Mock(status_code=400)
        response.json.return_value = {"error": "invalid_token"}
        client = GoogleCalendarClient(refresh_token="refresh-token")

        with patch("plane.integrations.google_calendar.client.requests.post", return_value=response) as post:
            client.revoke_grant()

        post.assert_called_once_with(
            "https://oauth2.googleapis.com/revoke",
            data={"token": "refresh-token"},
            timeout=10,
        )
        response.raise_for_status.assert_not_called()

    def test_revoke_rejects_a_genuine_bad_request(self):
        response = Mock(status_code=400)
        response.json.return_value = {"error": "invalid_request"}
        response.raise_for_status.side_effect = requests.HTTPError("provider response secret")
        client = GoogleCalendarClient(refresh_token="refresh-token")

        with (
            patch("plane.integrations.google_calendar.client.requests.post", return_value=response),
            pytest.raises(GoogleCalendarClientError, match="Google Calendar grant revocation failed") as error,
        ):
            client.revoke_grant()

        assert "secret" not in str(error.value)

    def test_expired_access_token_is_refreshed_before_calendar_creation(self):
        refresh_response = Mock(status_code=200)
        refresh_response.json.return_value = {"access_token": "fresh-access-token", "expires_in": 3600}
        create_response = Mock(status_code=200)
        create_response.json.return_value = {"id": "plane-calendar"}
        client = GoogleCalendarClient(
            access_token="expired-access-token",
            refresh_token="refresh-token",
            token_expires_at=timezone.now() - timedelta(minutes=1),
        )

        with (
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=GoogleCalendarOAuthCredentials("client-id", "client-secret"),
            ),
            patch("plane.integrations.google_calendar.client.requests.post", return_value=refresh_response) as post,
            patch(
                "plane.integrations.google_calendar.client.requests.request", return_value=create_response
            ) as request,
        ):
            client.create_calendar()

        post.assert_called_once_with(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": "client-id",
                "client_secret": "client-secret",
                "grant_type": "refresh_token",
                "refresh_token": "refresh-token",
            },
            timeout=10,
        )
        assert request.call_args.kwargs["headers"] == {"Authorization": "Bearer fresh-access-token"}
        assert client.access_token.value == "fresh-access-token"

    def test_provider_error_does_not_expose_response_content(self):
        response = Mock(status_code=500)
        response.raise_for_status.side_effect = requests.HTTPError("provider response secret")
        client = GoogleCalendarClient(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with (
            patch("plane.integrations.google_calendar.client.requests.request", return_value=response),
            pytest.raises(GoogleCalendarClientError, match="Google Calendar creation failed") as error,
        ):
            client.create_calendar()

        assert "secret" not in str(error.value)
