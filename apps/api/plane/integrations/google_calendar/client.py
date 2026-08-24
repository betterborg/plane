# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from django.utils import timezone

from plane.integrations.google_calendar.oauth import (
    GOOGLE_CALENDAR_OAUTH_TIMEOUT,
    GOOGLE_CALENDAR_REVOCATION_URL,
    GOOGLE_CALENDAR_TOKEN_URL,
    GoogleCalendarOAuthConfigurationError,
    get_google_calendar_oauth_credentials,
)
from plane.license.utils.google_calendar_credentials import google_calendar_credential_fingerprint


GOOGLE_CALENDAR_API_URL = "https://www.googleapis.com/calendar/v3"
GOOGLE_CALENDAR_SUMMARY = "Plane"
GOOGLE_CALENDAR_OPERATION_DESCRIPTION_PREFIX = "Plane calendar operation:"
GOOGLE_CALENDAR_TOKEN_EXPIRY_SKEW = timedelta(seconds=60)


class GoogleCalendarClientError(Exception):
    """Raised when a generation-scoped Google Calendar provider operation fails."""


class GoogleCalendarCredentialMismatch(GoogleCalendarClientError):
    """Raised before provider HTTP when the durable credential binding no longer matches."""

    classification = "oauth_credentials_changed"


class GoogleCalendarProviderError(GoogleCalendarClientError):
    """Raised when Google rejects a request or returns an unusable response."""

    classification = "provider_error"


class GoogleCalendarClientConflict(GoogleCalendarProviderError):
    """Raised when a deterministic provider event already exists."""


@dataclass(frozen=True)
class GoogleCalendarAccessToken:
    """The access token currently used by the Calendar client."""

    value: str
    expires_at: datetime


class GoogleCalendarClient:
    """Small client for Plane's recorded app-created Google calendar."""

    def __init__(
        self,
        *,
        credential_fingerprint,
        access_token="",
        refresh_token="",
        token_expires_at=None,
        persist_access_token=None,
    ):
        self._credential_fingerprint = credential_fingerprint
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expires_at = token_expires_at
        self._persist_access_token = persist_access_token

    @property
    def access_token(self):
        if not self._access_token or self._token_expires_at is None:
            return None
        return GoogleCalendarAccessToken(self._access_token, self._token_expires_at)

    def _validated_credentials(self):
        try:
            credentials = get_google_calendar_oauth_credentials()
        except GoogleCalendarOAuthConfigurationError as exc:
            raise GoogleCalendarCredentialMismatch("Google Calendar OAuth credentials changed") from exc
        effective_fingerprint = google_calendar_credential_fingerprint(
            credentials.client_id,
            credentials.client_secret,
        )
        if not self._credential_fingerprint or not hmac.compare_digest(
            self._credential_fingerprint,
            effective_fingerprint,
        ):
            raise GoogleCalendarCredentialMismatch("Google Calendar OAuth credentials changed")
        return credentials

    def validate_credentials(self):
        """Validate the durable credential binding without performing provider HTTP."""

        self._validated_credentials()

    def _refresh_access_token(self):
        credentials = self._validated_credentials()
        if not self._refresh_token:
            raise GoogleCalendarClientError("Google Calendar grant cannot refresh an access token")

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
            raise GoogleCalendarProviderError("Google Calendar access-token refresh failed") from exc

        self._access_token = access_token
        self._token_expires_at = timezone.now() + timedelta(seconds=expires_in)
        if self._persist_access_token is not None:
            self._persist_access_token(GoogleCalendarAccessToken(self._access_token, self._token_expires_at))

    def _ensure_access_token(self):
        access_token_is_current = bool(
            self._access_token
            and self._token_expires_at
            and self._token_expires_at > timezone.now() + GOOGLE_CALENDAR_TOKEN_EXPIRY_SKEW
        )
        refreshed = not access_token_is_current
        if refreshed:
            self._refresh_access_token()
        return self._access_token, refreshed

    def _send_calendar_request(self, method, path, access_token, **kwargs):
        self._validated_credentials()
        return requests.request(
            method,
            f"{GOOGLE_CALENDAR_API_URL}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=GOOGLE_CALENDAR_OAUTH_TIMEOUT,
            **kwargs,
        )

    def _calendar_request(self, method, path, **kwargs):
        try:
            access_token, refreshed = self._ensure_access_token()
            response = self._send_calendar_request(method, path, access_token, **kwargs)
            if response.status_code == 401 and self._refresh_token and not refreshed:
                self._refresh_access_token()
                response = self._send_calendar_request(method, path, self._access_token, **kwargs)
            return response
        except requests.RequestException as exc:
            raise GoogleCalendarProviderError("Google Calendar provider request failed") from exc

    @staticmethod
    def _operation_description(operation_id):
        return f"{GOOGLE_CALENDAR_OPERATION_DESCRIPTION_PREFIX} {operation_id}"

    def find_calendar(self, operation_id):
        """Find the app-created calendar carrying one durable operation marker."""

        page_token = None
        operation_description = self._operation_description(operation_id)
        while True:
            params = {"maxResults": 250, "minAccessRole": "owner"}
            if page_token:
                params["pageToken"] = page_token
            response = self._calendar_request("get", "/users/me/calendarList", params=params)
            try:
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Calendar list response is not an object")
                items = payload.get("items", [])
                if not isinstance(items, list):
                    raise ValueError("Calendar list response omitted a valid item list")
                for item in items:
                    if not isinstance(item, dict) or item.get("description") != operation_description:
                        continue
                    calendar_id = item.get("id", "")
                    if not isinstance(calendar_id, str) or not calendar_id or len(calendar_id) > 255:
                        raise ValueError("Calendar list response contained an invalid ID")
                    return calendar_id
                page_token = payload.get("nextPageToken")
                if page_token is None:
                    return None
                if not isinstance(page_token, str) or not page_token:
                    raise ValueError("Calendar list response contained an invalid page token")
            except (requests.RequestException, TypeError, ValueError) as exc:
                raise GoogleCalendarProviderError("Google Calendar recovery lookup failed") from exc

    def create_calendar(self, operation_id=None):
        """Create one dedicated app-owned calendar and return its provider ID."""

        payload = {"summary": GOOGLE_CALENDAR_SUMMARY}
        if operation_id is not None:
            payload["description"] = self._operation_description(operation_id)
        response = self._calendar_request("post", "/calendars", json=payload)
        try:
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Calendar response is not an object")
            calendar_id = payload.get("id", "")
            if not calendar_id or not isinstance(calendar_id, str) or len(calendar_id) > 255:
                raise ValueError("Calendar response omitted a valid ID")
        except (requests.RequestException, TypeError, ValueError) as exc:
            raise GoogleCalendarProviderError("Google Calendar creation failed") from exc
        return calendar_id

    def get_calendar(self, calendar_id):
        """Validate that the recorded dedicated calendar remains provider-readable."""

        if not calendar_id:
            raise GoogleCalendarClientError("Google Calendar lookup requires a recorded calendar ID")
        response = self._calendar_request("get", f"/calendars/{quote(calendar_id, safe='')}")
        try:
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("id") != calendar_id:
                raise ValueError("Calendar response did not match the recorded calendar")
            return payload
        except (requests.RequestException, TypeError, ValueError) as exc:
            raise GoogleCalendarProviderError("Google Calendar lookup failed") from exc

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
            raise GoogleCalendarProviderError("Google Calendar deletion failed") from exc

    @staticmethod
    def _event_path(calendar_id, event_id=""):
        path = f"/calendars/{quote(calendar_id, safe='')}/events"
        if event_id:
            path += f"/{quote(event_id, safe='')}"
        return path

    @staticmethod
    def _event_payload(response, error_message):
        try:
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Calendar event response is not an object")
            return payload
        except (requests.RequestException, TypeError, ValueError) as exc:
            raise GoogleCalendarProviderError(error_message) from exc

    def get_event(self, calendar_id, event_id):
        """Get one event, returning ``None`` when it is already absent."""

        response = self._calendar_request("get", self._event_path(calendar_id, event_id))
        if response.status_code == 404:
            return None
        return self._event_payload(response, "Google Calendar event lookup failed")

    def list_events(self, calendar_id, *, private_extended_property=None):
        """List all matching events across provider pages."""

        events = []
        page_token = None
        while True:
            params = {"maxResults": 250, "singleEvents": True}
            if private_extended_property:
                params["privateExtendedProperty"] = private_extended_property
            if page_token:
                params["pageToken"] = page_token
            response = self._calendar_request("get", self._event_path(calendar_id), params=params)
            payload = self._event_payload(response, "Google Calendar event list failed")
            items = payload.get("items", [])
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise GoogleCalendarProviderError("Google Calendar event list failed")
            events.extend(items)
            page_token = payload.get("nextPageToken")
            if page_token is None:
                return events
            if not isinstance(page_token, str) or not page_token:
                raise GoogleCalendarProviderError("Google Calendar event list failed")

    def insert_event(self, calendar_id, event_id, payload):
        """Insert an event with Plane's deterministic provider ID."""

        body = {**payload, "id": event_id}
        response = self._calendar_request("post", self._event_path(calendar_id), json=body)
        if response.status_code == 409:
            raise GoogleCalendarClientConflict("Google Calendar event already exists")
        return self._event_payload(response, "Google Calendar event insertion failed")

    def update_event(self, calendar_id, event_id, payload):
        """Replace an event using Google Calendar's full-update operation."""

        response = self._calendar_request("put", self._event_path(calendar_id, event_id), json=payload)
        return self._event_payload(response, "Google Calendar event update failed")

    def delete_event(self, calendar_id, event_id):
        """Delete an event, treating an already-absent event as converged."""

        response = self._calendar_request("delete", self._event_path(calendar_id, event_id))
        if response.status_code in {404, 410}:
            return
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise GoogleCalendarProviderError("Google Calendar event deletion failed") from exc

    def revoke_grant(self):
        """Revoke this connection's refresh grant, treating an absent grant as converged."""

        token = self._refresh_token or self._access_token
        if not token:
            return
        self._validated_credentials()
        try:
            response = requests.post(
                GOOGLE_CALENDAR_REVOCATION_URL,
                data={"token": token},
                timeout=GOOGLE_CALENDAR_OAUTH_TIMEOUT,
            )
            if response.status_code == 400:
                try:
                    payload = response.json()
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict) and payload.get("error") == "invalid_token":
                    return
            response.raise_for_status()
        except requests.RequestException as exc:
            raise GoogleCalendarProviderError("Google Calendar grant revocation failed") from exc
