# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from plane.integrations.google_calendar.oauth import (
    GOOGLE_CALENDAR_LIST_SCOPE,
    GOOGLE_CALENDAR_SCOPE,
    GoogleCalendarOAuthCredentials,
    GoogleCalendarOAuthGrant,
    GoogleCalendarOAuthIdentityError,
    GoogleCalendarOAuthScopeError,
    exchange_google_calendar_code,
    get_google_calendar_identity,
    validate_google_calendar_grant_scopes,
)


@pytest.mark.unit
class TestGoogleCalendarOAuthProvider:
    def test_exchange_uses_pkce_and_parses_complete_grant(self):
        credentials = GoogleCalendarOAuthCredentials("calendar-client", "calendar-secret")
        response = Mock()
        response.json.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "scope": f"openid email {GOOGLE_CALENDAR_SCOPE} {GOOGLE_CALENDAR_LIST_SCOPE}",
        }

        with patch("plane.integrations.google_calendar.oauth.requests.post", return_value=response) as post:
            checked_at = timezone.now()
            grant = exchange_google_calendar_code(
                credentials,
                code="authorization-code",
                code_verifier="pkce-verifier",
                redirect_uri="https://plane.example/auth/google-calendar/callback/",
            )

        validate_google_calendar_grant_scopes(grant)
        assert grant.access_token == "access-token"
        assert grant.refresh_token == "refresh-token"
        assert grant.token_expires_at > checked_at
        post.assert_called_once()
        assert post.call_args.kwargs["data"]["code_verifier"] == "pkce-verifier"
        assert post.call_args.kwargs["data"]["client_secret"] == "calendar-secret"

    def test_scope_validation_rejects_extra_or_missing_scope(self):
        for scopes in (
            frozenset({"openid", "email"}),
            frozenset({"openid", "email", GOOGLE_CALENDAR_SCOPE, GOOGLE_CALENDAR_LIST_SCOPE, "profile"}),
        ):
            grant = GoogleCalendarOAuthGrant(
                access_token="access-token",
                refresh_token="refresh-token",
                token_expires_at=timezone.now(),
                scopes=scopes,
            )

            with pytest.raises(GoogleCalendarOAuthScopeError):
                validate_google_calendar_grant_scopes(grant)

    def test_identity_requires_verified_email_and_uses_stable_subject(self):
        grant = GoogleCalendarOAuthGrant(
            access_token="access-token",
            refresh_token="refresh-token",
            token_expires_at=timezone.now(),
            scopes=frozenset({"openid", "email", GOOGLE_CALENDAR_SCOPE, GOOGLE_CALENDAR_LIST_SCOPE}),
        )
        response = Mock()
        response.json.return_value = {
            "sub": "stable-google-account",
            "email": "member@example.com",
            "email_verified": True,
        }

        with patch("plane.integrations.google_calendar.oauth.requests.get", return_value=response) as get:
            identity = get_google_calendar_identity(grant)

        assert identity.account_id == "stable-google-account"
        assert identity.email == "member@example.com"
        assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer access-token"}

        response.json.return_value["email_verified"] = False
        with (
            patch("plane.integrations.google_calendar.oauth.requests.get", return_value=response),
            pytest.raises(GoogleCalendarOAuthIdentityError),
        ):
            get_google_calendar_identity(grant)
