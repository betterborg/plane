# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import re
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
import requests
from bs4 import BeautifulSoup
from django.test import override_settings
from django.utils import timezone

from plane.bgtasks.google_calendar_task import (
    _mark_authorization_failure,
    send_google_calendar_disconnected_email,
)
from plane.db.models import GoogleCalendarConnection, Notification
from plane.integrations.google_calendar.client import (
    GoogleCalendarAuthorizationError,
    GoogleCalendarClient,
    GoogleCalendarInvalidGrant,
    GoogleCalendarProviderError,
)
from plane.integrations.google_calendar.lifecycle import (
    UnusableGoogleCalendarGrant,
    apply_google_calendar_oauth_success,
)
from plane.integrations.google_calendar.oauth import GOOGLE_CALENDAR_SCOPES, GoogleCalendarOAuthCredentials
from plane.license.utils.google_calendar_credentials import google_calendar_credential_fingerprint
from plane.tests.factories import GoogleCalendarConnectionFactory


OAUTH_CREDENTIALS = GoogleCalendarOAuthCredentials("client-id", "client-secret")
CREDENTIAL_FINGERPRINT = google_calendar_credential_fingerprint(
    OAUTH_CREDENTIALS.client_id,
    OAUTH_CREDENTIALS.client_secret,
)


def _client(**kwargs):
    return GoogleCalendarClient(credential_fingerprint=CREDENTIAL_FINGERPRINT, **kwargs)


@pytest.mark.unit
class TestGoogleCalendarAuthorizationClassification:
    @pytest.fixture(autouse=True)
    def _configured_oauth_client(self):
        with patch(
            "plane.integrations.google_calendar.client.get_google_calendar_oauth_credentials",
            return_value=OAUTH_CREDENTIALS,
        ):
            yield

    def test_invalid_refresh_grant_is_permanent_without_calendar_retry(self):
        invalid_grant = Mock(status_code=400, headers={})
        invalid_grant.json.return_value = {"error": "invalid_grant"}
        client = _client(
            access_token="expired-access-token",
            refresh_token="invalid-refresh-token",
            token_expires_at=timezone.now() - timedelta(minutes=1),
        )

        with (
            patch("plane.integrations.google_calendar.client.requests.post", return_value=invalid_grant) as post,
            patch("plane.integrations.google_calendar.client.requests.request") as request,
            pytest.raises(GoogleCalendarInvalidGrant) as error,
        ):
            client.get_calendar("plane-calendar")

        assert error.value.classification == "refresh_token_invalid"
        post.assert_called_once()
        request.assert_not_called()

    def test_authorization_that_persists_after_refresh_is_permanent(self):
        unauthorized = Mock(status_code=401, headers={})
        refreshed = Mock(status_code=200, headers={})
        refreshed.json.return_value = {"access_token": "new-access-token", "expires_in": 3600}
        client = _client(
            access_token="rejected-access-token",
            refresh_token="refresh-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

        with (
            patch("plane.integrations.google_calendar.client.requests.post", return_value=refreshed) as post,
            patch(
                "plane.integrations.google_calendar.client.requests.request",
                side_effect=[unauthorized, unauthorized],
            ) as request,
            pytest.raises(GoogleCalendarAuthorizationError) as error,
        ):
            client.get_calendar("plane-calendar")

        assert error.value.classification == "authorization_failed"
        post.assert_called_once()
        assert request.call_count == 2

    def test_transient_provider_failure_keeps_retry_classification(self):
        unavailable = Mock(status_code=503, headers={})
        unavailable.raise_for_status.side_effect = requests.HTTPError("unavailable")
        client = _client(
            access_token="access-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
            max_retries=0,
        )

        with (
            patch("plane.integrations.google_calendar.client.requests.request", return_value=unavailable),
            pytest.raises(GoogleCalendarProviderError) as error,
        ):
            client.get_calendar("plane-calendar")

        assert not isinstance(error.value, (GoogleCalendarInvalidGrant, GoogleCalendarAuthorizationError))
        assert error.value.classification == "provider_error"


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarBrokenNotice:
    @pytest.mark.parametrize(
        ("failure", "classification"),
        [
            (GoogleCalendarInvalidGrant("refresh rejected"), "refresh_token_invalid"),
            (GoogleCalendarAuthorizationError("authorization rejected"), "authorization_failed"),
        ],
        ids=["invalid-grant", "persistent-authorization"],
    )
    def test_first_broken_transition_creates_one_notification_and_email_task(self, failure, classification):
        connection = GoogleCalendarConnectionFactory(
            active=True,
            workspace_integration__workspace__slug="acme",
            workspace_integration__workspace__name="Acme",
        )

        with patch.object(send_google_calendar_disconnected_email, "run") as send_email:
            _mark_authorization_failure(connection, failure)
            connection.refresh_from_db()
            _mark_authorization_failure(connection, failure)

        connection.refresh_from_db()
        assert connection.status == GoogleCalendarConnection.Status.ERROR
        assert connection.last_error == classification
        assert connection.broken_notified_at is not None
        notification = Notification.objects.get(receiver=connection.member)
        assert notification.project_id is None
        assert notification.entity_name == "google_calendar_connection"
        assert notification.entity_identifier == connection.id
        assert notification.data == {
            "google_calendar_connection": {
                "id": str(connection.id),
                "provider_email": connection.provider_email,
                "status": "broken",
                "error": classification,
                "action_url": "/acme/settings/integrations/google-calendar",
            }
        }
        send_email.assert_called_once_with(connection.member.email, "Acme", "acme")

    def test_rejected_reconnect_preserves_health_until_usable_grant_is_validated(self):
        notified_at = timezone.now() - timedelta(hours=1)
        connection = GoogleCalendarConnectionFactory(
            bound_broken=True,
            broken_notified_at=notified_at,
            oauth_state="attempt",
            refresh_token="invalid-old-refresh-token",
        )

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            pytest.raises(UnusableGoogleCalendarGrant),
        ):
            apply_google_calendar_oauth_success(
                connection.id,
                connection.lifecycle_generation,
                provider_account_id=connection.provider_account_id,
                provider_email=connection.provider_email,
                access_token="partial-access-token",
                token_expires_at=timezone.now() + timedelta(hours=1),
                scopes=GOOGLE_CALENDAR_SCOPES,
                credential_fingerprint=CREDENTIAL_FINGERPRINT,
            )

        connection.refresh_from_db()
        assert connection.status == GoogleCalendarConnection.Status.ERROR
        assert connection.last_error == "refresh_token_invalid"
        assert connection.broken_notified_at == notified_at
        assert connection.oauth_state == "attempt"
        assert connection.refresh_token == "invalid-old-refresh-token"
        assert connection.access_token == ""

        with patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"):
            apply_google_calendar_oauth_success(
                connection.id,
                connection.lifecycle_generation,
                provider_account_id=connection.provider_account_id,
                provider_email=connection.provider_email,
                refresh_token="new-refresh-token",
                scopes=GOOGLE_CALENDAR_SCOPES,
                credential_fingerprint=CREDENTIAL_FINGERPRINT,
            )

        connection.refresh_from_db()
        assert connection.status == GoogleCalendarConnection.Status.PENDING
        assert connection.last_error == ""
        assert connection.broken_notified_at is None
        assert connection.oauth_state == ""


@pytest.mark.unit
class TestGoogleCalendarBrokenEmail:
    @override_settings(APP_BASE_URL="https://plane.example", WEB_URL="https://api.plane.example")
    def test_disconnected_email_html_and_text_link_to_member_settings(self, mailoutbox):
        with patch(
            "plane.bgtasks.google_calendar_task.get_email_configuration",
            return_value=(None, None, None, 587, "0", "0", "Plane <team@plane.example>"),
        ):
            send_google_calendar_disconnected_email.run("member@example.com", "Acme", "acme")

        assert len(mailoutbox) == 1
        email = mailoutbox[0]
        expected_url = "https://plane.example/acme/settings/integrations/google-calendar"
        text_action = re.search(r"Reconnect Google Calendar: (\S+)", email.body)
        assert text_action is not None
        assert text_action.group(1) == expected_url
        html = next(content for content, mime_type in email.alternatives if mime_type == "text/html")
        reconnect_link = BeautifulSoup(html, "html.parser").find("a", string="Reconnect Google Calendar")
        assert reconnect_link is not None
        assert reconnect_link["href"] == expected_url
