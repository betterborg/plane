# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import hashlib
import hmac

from django.conf import settings
from plane.db.models import GoogleCalendarConnection
from plane.integrations.google_calendar.lifecycle import lock_all_google_calendar_connections


GOOGLE_CALENDAR_CREDENTIAL_KEYS = frozenset(
    {
        "GOOGLE_CALENDAR_CLIENT_ID",
        "GOOGLE_CALENDAR_CLIENT_SECRET",
        "GOOGLE_CALENDAR_IS_PROJECT_DEDICATED",
    }
)

_GOOGLE_CALENDAR_PROVIDER_STATE_FIELDS = (
    "provider_account_id",
    "provider_email",
    "calendar_id",
    "calendar_operation_id",
    "access_token",
    "refresh_token",
    "sync_token",
    "page_token",
    "token_expires_at",
    "scopes",
)

_GOOGLE_CALENDAR_OAUTH_ATTEMPT_DEFAULTS = {
    "oauth_state": "",
    "oauth_code_verifier": "",
    "oauth_redirect_uri": "",
    "oauth_attempt_expires_at": None,
}


class GoogleCalendarCredentialsLocked(Exception):
    """Raised when dedicated Calendar credentials cannot be replaced safely."""


def google_calendar_credentials_are_locked(configuration_keys):
    """Return whether a configuration update would replace released Calendar credentials."""

    return settings.GOOGLE_CALENDAR_RELEASED and bool(GOOGLE_CALENDAR_CREDENTIAL_KEYS.intersection(configuration_keys))


def prepare_google_calendar_credential_replacement(configuration_keys):
    """Lock Calendar rows, reject provider state, and invalidate pending attempts."""

    if not GOOGLE_CALENDAR_CREDENTIAL_KEYS.intersection(configuration_keys):
        return
    if google_calendar_credentials_are_locked(configuration_keys):
        raise GoogleCalendarCredentialsLocked

    # Use the unfiltered manager so a soft-deleted grant can never make an
    # unsupported credential replacement appear safe.
    calendar_connections = lock_all_google_calendar_connections()
    if any(
        getattr(connection, field)
        for connection in calendar_connections
        for field in _GOOGLE_CALENDAR_PROVIDER_STATE_FIELDS
    ):
        raise GoogleCalendarCredentialsLocked

    # Lock every remaining attempt-only/tombstone row before invalidating it.
    # This update and the configuration bulk update share the caller's atomic
    # transaction, so a callback cannot retain valid attempt metadata after a
    # successful replacement.
    connection_ids = [connection.id for connection in calendar_connections]
    if connection_ids:
        GoogleCalendarConnection.all_objects.filter(id__in=connection_ids).update(
            **_GOOGLE_CALENDAR_OAUTH_ATTEMPT_DEFAULTS
        )


def google_calendar_credential_fingerprint(client_id, client_secret):
    """Return a stable keyed fingerprint without exposing either credential."""

    credential_material = f"{client_id}\0{client_secret}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), credential_material, hashlib.sha256).hexdigest()
