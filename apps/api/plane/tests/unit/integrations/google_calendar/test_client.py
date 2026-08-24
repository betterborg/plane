# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import UUID

import pytest
import requests
from django.utils import timezone

from plane.integrations.google_calendar.client import (
    GoogleCalendarClient,
    GoogleCalendarClientConflict,
    GoogleCalendarClientError,
    GoogleCalendarCredentialMismatch,
    GoogleCalendarProviderError,
)
from plane.integrations.google_calendar.oauth import GoogleCalendarOAuthCredentials
from plane.license.utils.google_calendar_credentials import google_calendar_credential_fingerprint


OAUTH_CREDENTIALS = GoogleCalendarOAuthCredentials("client-id", "client-secret")


def _client(**kwargs):
    return GoogleCalendarClient(
        credential_fingerprint=google_calendar_credential_fingerprint(
            OAUTH_CREDENTIALS.client_id,
            OAUTH_CREDENTIALS.client_secret,
        ),
        **kwargs,
    )


@pytest.mark.unit
class TestGoogleCalendarClient:
    @pytest.fixture(autouse=True)
    def _configured_oauth_client(self):
        with patch(
            "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
            return_value=OAUTH_CREDENTIALS,
        ):
            yield

    def test_event_crud_uses_encoded_paths_and_full_update(self):
        get_response = Mock(status_code=200)
        get_response.json.return_value = {"id": "event/id"}
        insert_response = Mock(status_code=200)
        insert_response.json.return_value = {"id": "event/id"}
        update_response = Mock(status_code=200)
        update_response.json.return_value = {"id": "event/id", "summary": "Updated"}
        delete_response = Mock(status_code=204)
        client = _client(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with patch(
            "plane.integrations.google_calendar.client.requests.request",
            side_effect=[get_response, insert_response, update_response, delete_response],
        ) as request:
            assert client.get_event("calendar/id", "event/id") == {"id": "event/id"}
            client.insert_event("calendar/id", "event/id", {"summary": "Created"})
            client.update_event("calendar/id", "event/id", {"summary": "Updated"})
            client.delete_event("calendar/id", "event/id")

        event_path = "https://www.googleapis.com/calendar/v3/calendars/calendar%2Fid/events/event%2Fid"
        assert request.call_args_list[0].args[:2] == ("get", event_path)
        assert request.call_args_list[1].args[:2] == (
            "post",
            "https://www.googleapis.com/calendar/v3/calendars/calendar%2Fid/events",
        )
        assert request.call_args_list[1].kwargs["json"] == {"summary": "Created", "id": "event/id"}
        assert request.call_args_list[2].args[:2] == ("put", event_path)
        assert request.call_args_list[3].args[:2] == ("delete", event_path)

    def test_event_list_paginates_with_a_private_marker(self):
        first_response = Mock(status_code=200)
        first_response.json.return_value = {"items": [{"id": "first"}], "nextPageToken": "next"}
        second_response = Mock(status_code=200)
        second_response.json.return_value = {"items": [{"id": "second"}]}
        client = _client(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with patch(
            "plane.integrations.google_calendar.client.requests.request",
            side_effect=[first_response, second_response],
        ) as request:
            events = client.list_events("calendar", private_extended_property="plane_entity_id=issue-id")

        assert events == [{"id": "first"}, {"id": "second"}]
        assert request.call_args_list[0].kwargs["params"] == {
            "maxResults": 250,
            "singleEvents": True,
            "privateExtendedProperty": "plane_entity_id=issue-id",
        }
        assert request.call_args_list[1].kwargs["params"]["pageToken"] == "next"

    def test_event_insert_surfaces_a_conflict_without_provider_content(self):
        response = Mock(status_code=409)
        client = _client(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with (
            patch("plane.integrations.google_calendar.client.requests.request", return_value=response),
            pytest.raises(GoogleCalendarClientConflict, match="already exists"),
        ):
            client.insert_event("calendar", "event", {"summary": "Created"})

    @pytest.mark.parametrize("status_code", [404, 410])
    def test_event_delete_treats_provider_absence_as_converged(self, status_code):
        response = Mock(status_code=status_code)
        client = _client(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with patch("plane.integrations.google_calendar.client.requests.request", return_value=response):
            client.delete_event("calendar", "event")

        response.raise_for_status.assert_not_called()

    def test_create_uses_the_dedicated_calendar_endpoint(self):
        response = Mock(status_code=200)
        response.json.return_value = {"id": "plane-calendar@example.com"}
        client = _client(
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
        client = _client(
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
        client = _client(
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
        client = _client(
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
        client = _client(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with patch("plane.integrations.google_calendar.client.requests.request", return_value=response):
            client.delete_calendar("recorded-calendar")

        response.raise_for_status.assert_not_called()

    def test_revoke_treats_only_invalid_token_as_already_converged(self):
        response = Mock(status_code=400)
        response.json.return_value = {"error": "invalid_token"}
        client = _client(refresh_token="refresh-token")

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
        client = _client(refresh_token="refresh-token")

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
        client = _client(
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
        client = _client(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with (
            patch("plane.integrations.google_calendar.client.requests.request", return_value=response),
            pytest.raises(GoogleCalendarClientError, match="Google Calendar creation failed") as error,
        ):
            client.create_calendar()

        assert "secret" not in str(error.value)

    @pytest.mark.parametrize(
        "operation",
        [
            lambda client: client.create_calendar(),
            lambda client: client.get_calendar("calendar-id"),
            lambda client: client.delete_calendar("calendar-id"),
            lambda client: client.find_calendar(UUID("12345678-1234-5678-1234-567812345678")),
            lambda client: client.get_event("calendar-id", "event-id"),
            lambda client: client.list_events("calendar-id"),
            lambda client: client.insert_event("calendar-id", "event-id", {}),
            lambda client: client.update_event("calendar-id", "event-id", {}),
            lambda client: client.delete_event("calendar-id", "event-id"),
            lambda client: client.revoke_grant(),
        ],
    )
    def test_credential_mismatch_precedes_every_provider_operation(self, operation, caplog):
        stored_fingerprint = google_calendar_credential_fingerprint("original-id", "original-secret")
        client = GoogleCalendarClient(
            credential_fingerprint=stored_fingerprint,
            access_token="private-access-token",
            refresh_token="private-refresh-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with (
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=GoogleCalendarOAuthCredentials("changed-id", "changed-secret"),
            ),
            patch("plane.integrations.google_calendar.client.requests.post") as post,
            patch("plane.integrations.google_calendar.client.requests.request") as request,
            pytest.raises(GoogleCalendarCredentialMismatch) as error,
        ):
            operation(client)

        assert error.value.classification == "oauth_credentials_changed"
        post.assert_not_called()
        request.assert_not_called()
        assert "original-id" not in caplog.text
        assert "original-secret" not in caplog.text
        assert "changed-id" not in caplog.text
        assert "changed-secret" not in caplog.text
        assert stored_fingerprint not in caplog.text

    def test_credential_mismatch_precedes_access_token_refresh(self):
        client = GoogleCalendarClient(
            credential_fingerprint=google_calendar_credential_fingerprint("original-id", "original-secret"),
            refresh_token="private-refresh-token",
        )

        with (
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=GoogleCalendarOAuthCredentials("changed-id", "changed-secret"),
            ),
            patch("plane.integrations.google_calendar.client.requests.post") as post,
            patch("plane.integrations.google_calendar.client.requests.request") as request,
            pytest.raises(GoogleCalendarCredentialMismatch),
        ):
            client.create_calendar()

        post.assert_not_called()
        request.assert_not_called()

    def test_access_token_only_refresh_path_validates_credentials_before_token_usability(self):
        client = GoogleCalendarClient(
            credential_fingerprint=google_calendar_credential_fingerprint("original-id", "original-secret"),
            access_token="private-access-token",
            token_expires_at=timezone.now() + timedelta(seconds=30),
        )

        with (
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=GoogleCalendarOAuthCredentials("changed-id", "changed-secret"),
            ),
            patch("plane.integrations.google_calendar.client.requests.post") as post,
            patch("plane.integrations.google_calendar.client.requests.request") as request,
            pytest.raises(GoogleCalendarCredentialMismatch),
        ):
            client.create_calendar()

        post.assert_not_called()
        request.assert_not_called()

    def test_restoring_exact_credentials_allows_cleanup_to_resume(self):
        delete_response = Mock(status_code=204)
        stored_fingerprint = google_calendar_credential_fingerprint("original-id", "original-secret")
        client = GoogleCalendarClient(
            credential_fingerprint=stored_fingerprint,
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with (
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=GoogleCalendarOAuthCredentials("changed-id", "changed-secret"),
            ),
            patch("plane.integrations.google_calendar.client.requests.request") as request,
            pytest.raises(GoogleCalendarCredentialMismatch),
        ):
            client.delete_calendar("calendar-id")
        request.assert_not_called()

        with (
            patch(
                "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
                return_value=GoogleCalendarOAuthCredentials("original-id", "original-secret"),
            ),
            patch(
                "plane.integrations.google_calendar.client.requests.request",
                return_value=delete_response,
            ) as request,
        ):
            client.delete_calendar("calendar-id")

        request.assert_called_once()

    def test_unauthorized_request_refreshes_once_and_persists_before_retry(self):
        unauthorized_response = Mock(status_code=401)
        success_response = Mock(status_code=200)
        success_response.json.return_value = {"id": "calendar-id"}
        refresh_response = Mock(status_code=200)
        refresh_response.json.return_value = {"access_token": "fresh-token", "expires_in": 3600}
        persist_access_token = Mock()
        client = _client(
            access_token="old-token",
            refresh_token="refresh-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
            persist_access_token=persist_access_token,
        )

        with (
            patch("plane.integrations.google_calendar.client.requests.post", return_value=refresh_response) as post,
            patch(
                "plane.integrations.google_calendar.client.requests.request",
                side_effect=[unauthorized_response, success_response],
            ) as request,
        ):
            assert client.get_calendar("calendar-id") == {"id": "calendar-id"}

        post.assert_called_once()
        assert request.call_count == 2
        persist_access_token.assert_called_once()
        assert persist_access_token.call_args.args[0].value == "fresh-token"

    def test_provider_failures_have_a_stable_classification(self):
        response = Mock(status_code=500)
        response.raise_for_status.side_effect = requests.HTTPError("private provider response")
        client = _client(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with (
            patch("plane.integrations.google_calendar.client.requests.request", return_value=response),
            pytest.raises(GoogleCalendarProviderError) as error,
        ):
            client.get_calendar("calendar-id")

        assert error.value.classification == "provider_error"
