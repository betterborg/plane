# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import base64
import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Q
from django.utils import timezone

from plane.db.models import GoogleCalendarConnection
from plane.license.utils.instance_value import get_configuration_value


logger = logging.getLogger(__name__)

GOOGLE_CALENDAR_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_CALENDAR_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_CALENDAR_REVOCATION_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
GOOGLE_CALENDAR_SCOPES = frozenset({"openid", "email", GOOGLE_CALENDAR_SCOPE})
GOOGLE_CALENDAR_OAUTH_TIMEOUT = 10


class GoogleCalendarOAuthError(Exception):
    """Base error for a rejected Google Calendar OAuth response."""


class GoogleCalendarOAuthExchangeError(GoogleCalendarOAuthError):
    """Raised when Google does not return a usable token response."""


class GoogleCalendarOAuthScopeError(GoogleCalendarOAuthError):
    """Raised when the returned grant does not contain exactly the planned scopes."""


class GoogleCalendarOAuthIdentityError(GoogleCalendarOAuthError):
    """Raised when the returned grant cannot be bound to a verified Google identity."""


class GoogleCalendarOAuthConfigurationError(GoogleCalendarOAuthError):
    """Raised when the dedicated Calendar OAuth client is not completely configured."""


@dataclass(frozen=True)
class GoogleCalendarOAuthCredentials:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class GoogleCalendarOAuthGrant:
    access_token: str
    refresh_token: str
    token_expires_at: datetime
    scopes: frozenset[str]


@dataclass(frozen=True)
class GoogleCalendarOAuthIdentity:
    account_id: str
    email: str


def get_google_calendar_oauth_credentials():
    """Return the dedicated Calendar client credentials or fail closed."""

    client_id, client_secret, project_is_dedicated = get_configuration_value(
        [
            {"key": "GOOGLE_CALENDAR_CLIENT_ID", "default": os.environ.get("GOOGLE_CALENDAR_CLIENT_ID", "")},
            {
                "key": "GOOGLE_CALENDAR_CLIENT_SECRET",
                "default": os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET", ""),
            },
            {
                "key": "GOOGLE_CALENDAR_IS_PROJECT_DEDICATED",
                "default": os.environ.get("GOOGLE_CALENDAR_IS_PROJECT_DEDICATED", "0"),
            },
        ]
    )
    if not client_id or not client_secret or project_is_dedicated != "1":
        raise GoogleCalendarOAuthConfigurationError("Google Calendar OAuth credentials are incomplete")
    return GoogleCalendarOAuthCredentials(client_id=client_id, client_secret=client_secret)


def create_google_calendar_pkce_pair():
    """Create an RFC 7636 S256 verifier and challenge."""

    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_google_calendar_consent_url(credentials, *, redirect_uri, state, code_challenge):
    """Build the dedicated, offline Google Calendar consent request."""

    params = {
        "access_type": "offline",
        "client_id": credentials.client_id,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "include_granted_scopes": "false",
        "prompt": "consent",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(sorted(GOOGLE_CALENDAR_SCOPES)),
        "state": state,
    }
    return f"{GOOGLE_CALENDAR_AUTHORIZATION_URL}?{urlencode(params)}"


def exchange_google_calendar_code(credentials, *, code, code_verifier, redirect_uri):
    """Exchange one authorization code for a parsed temporary grant."""

    try:
        response = requests.post(
            GOOGLE_CALENDAR_TOKEN_URL,
            data={
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=GOOGLE_CALENDAR_OAUTH_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Token response is not an object")
        access_token = payload.get("access_token", "")
        expires_in = int(payload.get("expires_in", 0))
        scopes = frozenset(str(payload.get("scope", "")).split())
        if not access_token or expires_in <= 0:
            raise ValueError("Token response omitted a usable access token")
    except (requests.RequestException, TypeError, ValueError) as exc:
        raise GoogleCalendarOAuthExchangeError("Google Calendar token exchange failed") from exc

    return GoogleCalendarOAuthGrant(
        access_token=access_token,
        refresh_token=payload.get("refresh_token") or "",
        token_expires_at=timezone.now() + timedelta(seconds=expires_in),
        scopes=scopes,
    )


def validate_google_calendar_grant_scopes(grant):
    """Require openid, email, and exactly the app-created Calendar scope."""

    if grant.scopes != GOOGLE_CALENDAR_SCOPES:
        raise GoogleCalendarOAuthScopeError("Google Calendar consent did not return the complete planned scope set")


def get_google_calendar_identity(grant):
    """Resolve a verified, stable Google account identity for a grant."""

    try:
        response = requests.get(
            GOOGLE_CALENDAR_USERINFO_URL,
            headers={"Authorization": f"Bearer {grant.access_token}"},
            timeout=GOOGLE_CALENDAR_OAUTH_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("UserInfo response is not an object")
        account_id = payload.get("sub", "")
        email = payload.get("email", "")
        if (
            not account_id
            or len(account_id) > 255
            or not email
            or len(email) > 254
            or payload.get("email_verified") is not True
        ):
            raise ValueError("UserInfo omitted a verified account identity")
        validate_email(email)
    except (requests.RequestException, TypeError, ValueError, ValidationError) as exc:
        raise GoogleCalendarOAuthIdentityError("Google Calendar identity lookup failed") from exc
    return GoogleCalendarOAuthIdentity(account_id=account_id, email=email)


def google_calendar_account_has_live_grant(account_id):
    """Return whether revoking a temporary grant could affect a durable connection."""

    return (
        GoogleCalendarConnection.objects.select_for_update()
        .filter(provider_account_id=account_id)
        .filter(Q(access_token__gt="") | Q(refresh_token__gt=""))
        .exists()
    )


def revoke_rejected_google_calendar_grant(grant, *, account_id=""):
    """Best-effort revoke a rejected grant unless its account is already in durable use."""

    if account_id and google_calendar_account_has_live_grant(account_id):
        return False
    token = grant.refresh_token or grant.access_token
    if not token:
        return False
    try:
        response = requests.post(
            GOOGLE_CALENDAR_REVOCATION_URL,
            data={"token": token},
            timeout=GOOGLE_CALENDAR_OAUTH_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.warning("Failed to revoke a rejected Google Calendar OAuth grant", exc_info=True)
        return False
    return True
