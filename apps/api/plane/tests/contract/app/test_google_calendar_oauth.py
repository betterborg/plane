# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.db import close_old_connections, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from plane.app.views.google_calendar_oauth import (
    GOOGLE_CALENDAR_OAUTH_SESSION_KEY,
    StaleGoogleCalendarOAuthAttempt,
    _complete_callback,
)
from plane.bgtasks.google_calendar_task import reconcile_google_calendar_connection
from plane.db.models import GoogleCalendarConnection, WorkspaceMember
from plane.integrations.google_calendar.lifecycle import (
    lock_google_calendar_connection,
    request_google_calendar_disconnect,
)
from plane.integrations.google_calendar.oauth import (
    GOOGLE_CALENDAR_LIST_SCOPE,
    GOOGLE_CALENDAR_SCOPE,
    GOOGLE_CALENDAR_SCOPES,
    GoogleCalendarOAuthCredentials,
    GoogleCalendarOAuthExchangeError,
    GoogleCalendarOAuthGrant,
    GoogleCalendarOAuthIdentity,
    GoogleCalendarOAuthIdentityError,
)
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


@pytest.fixture
def oauth_credentials():
    return GoogleCalendarOAuthCredentials(client_id="calendar-client", client_secret="calendar-secret")


@pytest.fixture
def complete_grant():
    return GoogleCalendarOAuthGrant(
        access_token="new-access-token",
        refresh_token="new-refresh-token",
        token_expires_at=timezone.now() + timedelta(hours=1),
        scopes=frozenset({"openid", "email", GOOGLE_CALENDAR_SCOPE, GOOGLE_CALENDAR_LIST_SCOPE}),
    )


def _start_url(workspace):
    return reverse("google-calendar-oauth-start", kwargs={"slug": workspace.slug})


def _callback_url():
    return reverse("google-calendar-oauth-callback")


def _state_from_response(response):
    return parse_qs(urlparse(response.url).query)["state"][0]


def _redirect_error(response):
    return parse_qs(urlparse(response.url).query).get("error", [""])[0]


def _start_consent(session_client, workspace, oauth_credentials):
    with (
        override_settings(GOOGLE_CALENDAR_RELEASED=True),
        patch(
            "plane.app.views.google_calendar_oauth.get_google_calendar_oauth_credentials",
            return_value=oauth_credentials,
        ),
        patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
    ):
        return session_client.get(_start_url(workspace))


@pytest.mark.contract
class TestGoogleCalendarOAuth:
    @pytest.mark.django_db
    def test_unreleased_calendar_cannot_start_consent(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
    ):
        with override_settings(GOOGLE_CALENDAR_RELEASED=False):
            response = session_client.get(_start_url(workspace))

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert not GoogleCalendarConnection.objects.exists()

    @pytest.mark.django_db
    def test_disabled_workspace_cannot_start_consent(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
        oauth_credentials,
    ):
        calendar_workspace_integration.config = {"enabled": False}
        calendar_workspace_integration.save(update_fields=["config", "updated_at"])

        response = _start_consent(session_client, workspace, oauth_credentials)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data == {"error": "google_calendar_disabled"}
        assert not GoogleCalendarConnection.objects.exists()

    @pytest.mark.django_db
    def test_nonterminal_cleanup_blocks_a_fresh_account_start(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
        oauth_credentials,
        create_user,
    ):
        GoogleCalendarConnectionFactory(
            workspace_integration=calendar_workspace_integration,
            member=create_user,
            provider_account_id="old-google-account",
            calendar_id="old-plane-calendar",
            refresh_token="old-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=6,
            last_error="Google Calendar deletion failed",
        )

        response = _start_consent(session_client, workspace, oauth_credentials)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data == {"error": "google_calendar_disconnect_in_progress"}

    @pytest.mark.django_db
    def test_start_constructs_exact_offline_pkce_consent_and_one_bounded_attempt(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
        oauth_credentials,
        create_user,
    ):
        first_response = _start_consent(session_client, workspace, oauth_credentials)
        first_state = _state_from_response(first_response)
        second_response = _start_consent(session_client, workspace, oauth_credentials)
        second_state = _state_from_response(second_response)

        assert first_response.status_code == status.HTTP_302_FOUND
        assert second_response.status_code == status.HTTP_302_FOUND
        assert first_state != second_state
        query = parse_qs(urlparse(second_response.url).query)
        assert set(query["scope"][0].split()) == {
            "openid",
            "email",
            GOOGLE_CALENDAR_SCOPE,
            GOOGLE_CALENDAR_LIST_SCOPE,
        }
        assert query["access_type"] == ["offline"]
        assert query["prompt"] == ["consent"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"] == ["http://testserver/auth/google-calendar/callback/"]
        connection = GoogleCalendarConnection.objects.get(member=create_user)
        assert connection.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        assert connection.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert connection.lifecycle_generation == 0
        assert len(session_client.session[GOOGLE_CALENDAR_OAUTH_SESSION_KEY]) == 5

    @pytest.mark.django_db
    def test_abandoned_restart_makes_old_callback_stale_without_clearing_new_attempt(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
        oauth_credentials,
    ):
        old_state = _state_from_response(_start_consent(session_client, workspace, oauth_credentials))
        new_state = _state_from_response(_start_consent(session_client, workspace, oauth_credentials))

        with override_settings(GOOGLE_CALENDAR_RELEASED=True, APP_BASE_URL="https://plane.example"):
            response = session_client.get(_callback_url(), {"state": old_state, "code": "old-code"})

        assert response.status_code == status.HTTP_302_FOUND
        assert _redirect_error(response) == "google_calendar_oauth_stale"
        connection = GoogleCalendarConnection.objects.get()
        assert connection.oauth_state == session_client.session[GOOGLE_CALENDAR_OAUTH_SESSION_KEY]["attempt_generation"]
        assert new_state

    @pytest.mark.django_db
    def test_expired_callback_does_not_exchange_or_advance_lifecycle(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
        oauth_credentials,
    ):
        callback_state = _state_from_response(_start_consent(session_client, workspace, oauth_credentials))
        connection = GoogleCalendarConnection.objects.get()
        connection.oauth_attempt_expires_at = timezone.now() - timedelta(seconds=1)
        connection.save(update_fields=["oauth_attempt_expires_at", "updated_at"])

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True, APP_BASE_URL="https://plane.example"),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.app.views.google_calendar_oauth.exchange_google_calendar_code") as exchange,
        ):
            response = session_client.get(_callback_url(), {"state": callback_state, "code": "expired-code"})

        assert _redirect_error(response) == "google_calendar_oauth_stale"
        exchange.assert_not_called()
        connection.refresh_from_db()
        assert connection.lifecycle_generation == 0
        assert not connection.provider_account_id

    @pytest.mark.django_db
    def test_exchange_identity_and_partial_consent_failures_leave_no_durable_grant(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
        oauth_credentials,
        complete_grant,
    ):
        failure_cases = (
            (
                {"exchange_google_calendar_code.side_effect": GoogleCalendarOAuthExchangeError("exchange")},
                "google_calendar_oauth_exchange_failed",
                False,
            ),
            (
                {
                    "exchange_google_calendar_code.return_value": GoogleCalendarOAuthGrant(
                        access_token="partial-access",
                        refresh_token="partial-refresh",
                        token_expires_at=complete_grant.token_expires_at,
                        scopes=frozenset({"openid", "email"}),
                    )
                },
                "google_calendar_oauth_incomplete_scope",
                True,
            ),
            (
                {
                    "exchange_google_calendar_code.return_value": complete_grant,
                    "get_google_calendar_identity.side_effect": GoogleCalendarOAuthIdentityError("identity"),
                },
                "google_calendar_oauth_identity_failed",
                True,
            ),
        )

        for configured_mocks, expected_error, should_revoke in failure_cases:
            callback_state = _state_from_response(_start_consent(session_client, workspace, oauth_credentials))
            exchange = Mock()
            identity = Mock(return_value=GoogleCalendarOAuthIdentity("google-account", "member@example.com"))
            if "exchange_google_calendar_code.side_effect" in configured_mocks:
                exchange.side_effect = configured_mocks["exchange_google_calendar_code.side_effect"]
            else:
                exchange.return_value = configured_mocks["exchange_google_calendar_code.return_value"]
            if "get_google_calendar_identity.side_effect" in configured_mocks:
                identity.side_effect = configured_mocks["get_google_calendar_identity.side_effect"]

            with (
                override_settings(GOOGLE_CALENDAR_RELEASED=True, APP_BASE_URL="https://plane.example"),
                patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
                patch(
                    "plane.app.views.google_calendar_oauth.get_google_calendar_oauth_credentials",
                    return_value=oauth_credentials,
                ),
                patch("plane.app.views.google_calendar_oauth.exchange_google_calendar_code", exchange),
                patch("plane.app.views.google_calendar_oauth.get_google_calendar_identity", identity),
                patch("plane.app.views.google_calendar_oauth.revoke_rejected_google_calendar_grant") as revoke,
            ):
                response = session_client.get(_callback_url(), {"state": callback_state, "code": "callback-code"})

            assert _redirect_error(response) == expected_error
            assert revoke.called is should_revoke
            connection = GoogleCalendarConnection.objects.get()
            assert connection.lifecycle_generation == 0
            assert not connection.provider_account_id
            assert not connection.access_token

    @pytest.mark.django_db(transaction=True)
    def test_complete_callback_persists_usable_grant_and_publishes_exact_generation(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
        oauth_credentials,
        complete_grant,
    ):
        callback_state = _state_from_response(_start_consent(session_client, workspace, oauth_credentials))
        lifecycle_task = Mock()
        lifecycle_task.delay.side_effect = RuntimeError("broker unavailable")
        identity = GoogleCalendarOAuthIdentity("google-account", "member@example.com")

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True, APP_BASE_URL="https://plane.example"),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.app.views.google_calendar_oauth.get_google_calendar_oauth_credentials",
                return_value=oauth_credentials,
            ),
            patch("plane.app.views.google_calendar_oauth.exchange_google_calendar_code", return_value=complete_grant),
            patch("plane.app.views.google_calendar_oauth.get_google_calendar_identity", return_value=identity),
            patch("plane.app.views.google_calendar_oauth.current_app.signature", return_value=lifecycle_task),
        ):
            response = session_client.get(_callback_url(), {"state": callback_state, "code": "callback-code"})

        assert response.status_code == status.HTTP_302_FOUND
        assert parse_qs(urlparse(response.url).query) == {"google_calendar_oauth": ["success"]}
        connection = GoogleCalendarConnection.objects.get()
        assert connection.provider_account_id == "google-account"
        assert connection.provider_email == "member@example.com"
        assert connection.access_token == complete_grant.access_token
        assert connection.refresh_token == complete_grant.refresh_token
        assert connection.desired_state == GoogleCalendarConnection.DesiredState.CONNECTED
        assert connection.status == GoogleCalendarConnection.Status.PENDING
        assert connection.lifecycle_generation == 1
        assert not connection.oauth_state
        lifecycle_task.delay.assert_called_once_with(str(connection.id), 1)

        with override_settings(GOOGLE_CALENDAR_RELEASED=True, APP_BASE_URL="https://plane.example"):
            replay = session_client.get(_callback_url(), {"state": callback_state, "code": "callback-code"})
        assert _redirect_error(replay) == "google_calendar_oauth_stale"
        connection.refresh_from_db()
        assert connection.lifecycle_generation == 1

    @pytest.mark.django_db(transaction=True)
    def test_callback_rechecks_active_membership_after_token_exchange(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
        oauth_credentials,
        complete_grant,
        create_user,
    ):
        callback_state = _state_from_response(_start_consent(session_client, workspace, oauth_credentials))
        lifecycle_task = Mock()

        def exchange_after_membership_removal(*args, **kwargs):
            WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(is_active=False)
            return complete_grant

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True, APP_BASE_URL="https://plane.example"),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.app.views.google_calendar_oauth.get_google_calendar_oauth_credentials",
                return_value=oauth_credentials,
            ),
            patch(
                "plane.app.views.google_calendar_oauth.exchange_google_calendar_code",
                side_effect=exchange_after_membership_removal,
            ),
            patch(
                "plane.app.views.google_calendar_oauth.get_google_calendar_identity",
                return_value=GoogleCalendarOAuthIdentity("google-account", "member@example.com"),
            ),
            patch("plane.app.views.google_calendar_oauth.current_app.signature", return_value=lifecycle_task),
            patch("plane.app.views.google_calendar_oauth.revoke_rejected_google_calendar_grant") as revoke,
        ):
            response = session_client.get(_callback_url(), {"state": callback_state, "code": "callback-code"})

        assert _redirect_error(response) == "google_calendar_oauth_stale"
        revoke.assert_called_once_with(complete_grant, account_id="google-account")
        lifecycle_task.delay.assert_not_called()
        connection = GoogleCalendarConnection.objects.get()
        assert connection.lifecycle_generation == 0
        assert not connection.provider_account_id
        assert not connection.access_token

    @pytest.mark.django_db(transaction=True)
    def test_callback_waits_for_revoke_and_cannot_commit_or_publish_stale_generation(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
        oauth_credentials,
        complete_grant,
        create_user,
    ):
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=calendar_workspace_integration,
            member=create_user,
            provider_account_id="google-account",
            provider_email="old@example.com",
            calendar_id="old-calendar",
            access_token="old-access-token",
            refresh_token="old-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
            lifecycle_generation=5,
        )
        _start_consent(session_client, workspace, oauth_credentials)
        payload = session_client.session[GOOGLE_CALENDAR_OAUTH_SESSION_KEY]
        revoke_locked = Barrier(2)
        revoke_can_finish = Event()
        callback_started = Event()
        identity = GoogleCalendarOAuthIdentity("google-account", "new@example.com")

        def revoke_connection():
            close_old_connections()
            try:
                with transaction.atomic():
                    locked_connection = lock_google_calendar_connection(connection.id)
                    revoke_locked.wait(timeout=5)
                    assert revoke_can_finish.wait(timeout=5)
                    return request_google_calendar_disconnect(
                        locked_connection.id,
                        locked_connection.lifecycle_generation,
                    )
            finally:
                close_old_connections()

        def complete_callback():
            close_old_connections()
            try:
                revoke_locked.wait(timeout=5)
                callback_started.set()
                request = SimpleNamespace(user=create_user)
                return _complete_callback(request, payload, complete_grant, identity)
            finally:
                close_old_connections()

        with patch("plane.app.views.google_calendar_oauth.enqueue_google_calendar_task_on_commit") as publish:
            with ThreadPoolExecutor(max_workers=2) as executor:
                revoke_future = executor.submit(revoke_connection)
                callback_future = executor.submit(complete_callback)
                assert callback_started.wait(timeout=5)
                assert callback_future.done() is False
                revoke_can_finish.set()
                revoke_command = revoke_future.result(timeout=5)
                with pytest.raises(StaleGoogleCalendarOAuthAttempt):
                    callback_future.result(timeout=5)

        assert revoke_command.connection_id == connection.id
        assert revoke_command.generation == 6
        publish.assert_not_called()
        connection.refresh_from_db()
        assert connection.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        assert connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert connection.lifecycle_generation == 6
        assert connection.provider_email == "old@example.com"
        assert connection.access_token == "old-access-token"
        assert connection.refresh_token == "old-refresh-token"
        assert not connection.oauth_state

    @pytest.mark.django_db
    def test_same_account_reconnect_retains_existing_refresh_token(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
        oauth_credentials,
        complete_grant,
        create_user,
    ):
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=calendar_workspace_integration,
            member=create_user,
            provider_account_id="google-account",
            provider_email="old@example.com",
            refresh_token="existing-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ERROR,
            lifecycle_generation=3,
        )
        callback_state = _state_from_response(_start_consent(session_client, workspace, oauth_credentials))
        access_only_grant = GoogleCalendarOAuthGrant(
            access_token=complete_grant.access_token,
            refresh_token="",
            token_expires_at=complete_grant.token_expires_at,
            scopes=complete_grant.scopes,
        )

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True, APP_BASE_URL="https://plane.example"),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.app.views.google_calendar_oauth.get_google_calendar_oauth_credentials",
                return_value=oauth_credentials,
            ),
            patch(
                "plane.app.views.google_calendar_oauth.exchange_google_calendar_code",
                return_value=access_only_grant,
            ),
            patch(
                "plane.app.views.google_calendar_oauth.get_google_calendar_identity",
                return_value=GoogleCalendarOAuthIdentity("google-account", "new@example.com"),
            ),
            patch("plane.app.views.google_calendar_oauth.current_app.signature", return_value=Mock()),
        ):
            response = session_client.get(_callback_url(), {"state": callback_state, "code": "reconnect-code"})

        assert not _redirect_error(response)
        connection.refresh_from_db()
        assert connection.refresh_token == "existing-refresh-token"
        assert connection.provider_email == "new@example.com"
        assert connection.lifecycle_generation == 4

    @pytest.mark.django_db
    def test_different_account_preserves_old_connection_and_disposes_only_unshared_grant(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
        oauth_credentials,
        complete_grant,
        create_user,
    ):
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=calendar_workspace_integration,
            member=create_user,
            provider_account_id="old-google-account",
            provider_email="old@example.com",
            calendar_id="old-calendar",
            access_token="old-access-token",
            refresh_token="old-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
            lifecycle_generation=5,
        )
        callback_state = _state_from_response(_start_consent(session_client, workspace, oauth_credentials))

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True, APP_BASE_URL="https://plane.example"),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.app.views.google_calendar_oauth.get_google_calendar_oauth_credentials",
                return_value=oauth_credentials,
            ),
            patch("plane.app.views.google_calendar_oauth.exchange_google_calendar_code", return_value=complete_grant),
            patch(
                "plane.app.views.google_calendar_oauth.get_google_calendar_identity",
                return_value=GoogleCalendarOAuthIdentity("new-google-account", "new@example.com"),
            ),
            patch("plane.app.views.google_calendar_oauth.revoke_rejected_google_calendar_grant") as revoke,
        ):
            response = session_client.get(_callback_url(), {"state": callback_state, "code": "switch-code"})

        assert _redirect_error(response) == "account_switch_requires_disconnect"
        revoke.assert_called_once_with(complete_grant)
        connection.refresh_from_db()
        assert connection.provider_account_id == "old-google-account"
        assert connection.provider_email == "old@example.com"
        assert connection.calendar_id == "old-calendar"
        assert connection.access_token == "old-access-token"
        assert connection.refresh_token == "old-refresh-token"
        assert connection.status == GoogleCalendarConnection.Status.ACTIVE
        assert connection.lifecycle_generation == 5

    @pytest.mark.django_db(transaction=True)
    def test_complete_account_switch_crosses_oauth_cleanup_tombstone_and_reprovisioning(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
        oauth_credentials,
        complete_grant,
        create_user,
    ):
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=calendar_workspace_integration,
            member=create_user,
            provider_account_id="old-google-account",
            provider_email="old@example.com",
            calendar_id="old-plane-calendar",
            access_token="old-access-token",
            refresh_token="old-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
            lifecycle_generation=8,
        )
        lifecycle_task = Mock()
        reconnect_client = Mock(access_token=None)
        delete_client = Mock(access_token=None)
        revoke_client = Mock(access_token=None)
        create_client = Mock(access_token=None)
        create_client.find_calendar.return_value = None
        create_client.create_calendar.return_value = "new-plane-calendar"

        def complete_callback(grant, identity, code):
            callback_state = _state_from_response(_start_consent(session_client, workspace, oauth_credentials))
            with (
                override_settings(GOOGLE_CALENDAR_RELEASED=True, APP_BASE_URL="https://plane.example"),
                patch(
                    "plane.app.views.google_calendar_oauth.get_google_calendar_oauth_credentials",
                    return_value=oauth_credentials,
                ),
                patch("plane.app.views.google_calendar_oauth.exchange_google_calendar_code", return_value=grant),
                patch("plane.app.views.google_calendar_oauth.get_google_calendar_identity", return_value=identity),
                patch("plane.app.views.google_calendar_oauth.current_app.signature", return_value=lifecycle_task),
            ):
                return session_client.get(_callback_url(), {"state": callback_state, "code": code})

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.app.views.google_calendar_oauth.revoke_rejected_google_calendar_grant") as reject_grant,
            patch(
                "plane.bgtasks.google_calendar_task.GoogleCalendarClient",
                side_effect=[reconnect_client, delete_client, revoke_client, create_client],
            ),
        ):
            mismatch_response = complete_callback(
                complete_grant,
                GoogleCalendarOAuthIdentity("new-google-account", "new@example.com"),
                "mismatch-code",
            )

            assert _redirect_error(mismatch_response) == "account_switch_requires_disconnect"
            reject_grant.assert_called_once_with(complete_grant)
            connection.refresh_from_db()
            assert connection.provider_account_id == "old-google-account"
            assert connection.calendar_id == "old-plane-calendar"
            assert connection.lifecycle_generation == 8

            old_account_grant = GoogleCalendarOAuthGrant(
                access_token="reconnected-access-token",
                refresh_token="reconnected-refresh-token",
                token_expires_at=complete_grant.token_expires_at,
                scopes=GOOGLE_CALENDAR_SCOPES,
            )
            reconnect_response = complete_callback(
                old_account_grant,
                GoogleCalendarOAuthIdentity("old-google-account", "old-reconnected@example.com"),
                "reconnect-code",
            )

            assert not _redirect_error(reconnect_response)
            connection.refresh_from_db()
            assert reconcile_google_calendar_connection(str(connection.id), connection.lifecycle_generation) == "active"

            connection.refresh_from_db()
            disconnect = request_google_calendar_disconnect(connection.id, connection.lifecycle_generation)
            assert disconnect is not None
            assert reconcile_google_calendar_connection(str(connection.id), disconnect.generation) == "disconnected"

            connection.refresh_from_db()
            assert connection.provider_account_id == ""
            assert connection.provider_email == ""
            assert connection.calendar_id == ""
            assert connection.access_token == ""
            assert connection.refresh_token == ""
            assert connection.status == GoogleCalendarConnection.Status.DISCONNECTED

            new_account_grant = GoogleCalendarOAuthGrant(
                access_token="new-access-token",
                refresh_token="new-refresh-token",
                token_expires_at=complete_grant.token_expires_at,
                scopes=GOOGLE_CALENDAR_SCOPES,
            )
            new_account_response = complete_callback(
                new_account_grant,
                GoogleCalendarOAuthIdentity("new-google-account", "new@example.com"),
                "new-account-code",
            )

            assert not _redirect_error(new_account_response)
            connection.refresh_from_db()
            assert reconcile_google_calendar_connection(str(connection.id), connection.lifecycle_generation) == "active"

        connection.refresh_from_db()
        reconnect_client.create_calendar.assert_not_called()
        delete_client.delete_calendar.assert_called_once_with("old-plane-calendar")
        revoke_client.revoke_grant.assert_called_once_with()
        create_client.create_calendar.assert_called_once()
        assert connection.provider_account_id == "new-google-account"
        assert connection.calendar_id == "new-plane-calendar"
        assert connection.status == GoogleCalendarConnection.Status.ACTIVE

    @pytest.mark.django_db
    def test_different_account_does_not_revoke_grant_used_by_another_connection(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
        oauth_credentials,
        complete_grant,
        create_user,
    ):
        GoogleCalendarConnectionFactory(
            workspace_integration=calendar_workspace_integration,
            member=create_user,
            provider_account_id="old-google-account",
            refresh_token="old-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
        )
        other_workspace_integration = WorkspaceIntegrationFactory(
            integration=calendar_workspace_integration.integration,
            config={"enabled": True},
        )
        GoogleCalendarConnectionFactory(
            workspace_integration=other_workspace_integration,
            member=UserFactory(),
            provider_account_id="shared-new-account",
            refresh_token="shared-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
        )
        callback_state = _state_from_response(_start_consent(session_client, workspace, oauth_credentials))

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True, APP_BASE_URL="https://plane.example"),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.app.views.google_calendar_oauth.get_google_calendar_oauth_credentials",
                return_value=oauth_credentials,
            ),
            patch("plane.app.views.google_calendar_oauth.exchange_google_calendar_code", return_value=complete_grant),
            patch(
                "plane.app.views.google_calendar_oauth.get_google_calendar_identity",
                return_value=GoogleCalendarOAuthIdentity("shared-new-account", "shared@example.com"),
            ),
            patch("plane.app.views.google_calendar_oauth.revoke_rejected_google_calendar_grant") as revoke,
        ):
            response = session_client.get(_callback_url(), {"state": callback_state, "code": "switch-code"})

        assert _redirect_error(response) == "account_switch_requires_disconnect"
        revoke.assert_not_called()
