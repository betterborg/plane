# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from django.utils import timezone

from plane.integrations.google_calendar.oauth import (
    GOOGLE_CALENDAR_OAUTH_TIMEOUT,
    GOOGLE_CALENDAR_REVOCATION_URL,
    GOOGLE_CALENDAR_TOKEN_URL,
    get_google_calendar_oauth_credentials,
)


GOOGLE_CALENDAR_API_URL = "https://www.googleapis.com/calendar/v3"
GOOGLE_CALENDAR_SUMMARY = "Plane"
GOOGLE_CALENDAR_TOKEN_EXPIRY_SKEW = timedelta(seconds=60)


class GoogleCalendarClientError(Exception):
    """Raised when a generation-scoped Google Calendar provider operation fails."""


@dataclass(frozen=True)
class GoogleCalendarAccessToken:
    """The access token currently used by the Calendar client."""

    value: str
    expires_at: datetime


class GoogleCalendarClient:
    """Small client for Plane's recorded app-created Google calendar."""

    def __init__(self, *, access_token="", refresh_token="", token_expires_at=None):
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expires_at = token_expires_at

    @property
    def access_token(self):
        if not self._access_token or self._token_expires_at is None:
            return None
        return GoogleCalendarAccessToken(self._access_token, self._token_expires_at)

    def _refresh_access_token(self):
        if not self._refresh_token:
            raise GoogleCalendarClientError("Google Calendar grant cannot refresh an access token")

        credentials = get_google_calendar_oauth_credentials()
        try:
            response = requests.post(
                GOOGLE_CALENDAR_TOKEN_URL,
                data={
                    "client_id": credentials.client_id,
                    "client_secret": credentials.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
                timeout=GOOGLE_CALENDAR_OAUTH_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Token response is not an object")
            access_token = payload.get("access_token", "")
            expires_in = int(payload.get("expires_in", 0))
            if not access_token or expires_in <= 0:
                raise ValueError("Token response omitted a usable access token")
        except (requests.RequestException, TypeError, ValueError) as exc:
            raise GoogleCalendarClientError("Google Calendar access-token refresh failed") from exc

        self._access_token = access_token
        self._token_expires_at = timezone.now() + timedelta(seconds=expires_in)

    def _ensure_access_token(self, *, force_refresh=False):
        access_token_is_current = bool(
            self._access_token
            and self._token_expires_at
            and self._token_expires_at > timezone.now() + GOOGLE_CALENDAR_TOKEN_EXPIRY_SKEW
        )
        if force_refresh or not access_token_is_current:
            self._refresh_access_token()
        return self._access_token

    def _calendar_request(self, method, path, **kwargs):
        access_token = self._ensure_access_token()
        try:
            response = requests.request(
                method,
                f"{GOOGLE_CALENDAR_API_URL}{path}",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=GOOGLE_CALENDAR_OAUTH_TIMEOUT,
                **kwargs,
            )
            if response.status_code == 401 and self._refresh_token:
                access_token = self._ensure_access_token(force_refresh=True)
                response = requests.request(
                    method,
                    f"{GOOGLE_CALENDAR_API_URL}{path}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=GOOGLE_CALENDAR_OAUTH_TIMEOUT,
                    **kwargs,
                )
            return response
        except requests.RequestException as exc:
            raise GoogleCalendarClientError("Google Calendar provider request failed") from exc

    def create_calendar(self):
        """Create one dedicated app-owned calendar and return its provider ID."""

        response = self._calendar_request("post", "/calendars", json={"summary": GOOGLE_CALENDAR_SUMMARY})
        try:
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Calendar response is not an object")
            calendar_id = payload.get("id", "")
            if not calendar_id or not isinstance(calendar_id, str) or len(calendar_id) > 255:
                raise ValueError("Calendar response omitted a valid ID")
        except (requests.RequestException, TypeError, ValueError) as exc:
            raise GoogleCalendarClientError("Google Calendar creation failed") from exc
        return calendar_id

    def delete_calendar(self, calendar_id):
        """Delete only the durable calendar ID supplied by the caller."""

        if not calendar_id:
            raise GoogleCalendarClientError("Google Calendar deletion requires a recorded calendar ID")
        response = self._calendar_request("delete", f"/calendars/{quote(calendar_id, safe='')}")
        if response.status_code == 404:
            return
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise GoogleCalendarClientError("Google Calendar deletion failed") from exc

    def revoke_grant(self):
        """Revoke this connection's refresh grant, treating an absent grant as converged."""

        token = self._refresh_token or self._access_token
        if not token:
            return
        try:
            response = requests.post(
                GOOGLE_CALENDAR_REVOCATION_URL,
                data={"token": token},
                timeout=GOOGLE_CALENDAR_OAUTH_TIMEOUT,
            )
            if response.status_code == 400:
                return
            response.raise_for_status()
        except requests.RequestException as exc:
            raise GoogleCalendarClientError("Google Calendar grant revocation failed") from exc
