# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta

GOOGLE_CALENDAR_HEALTHY_RECONCILIATION_OVERDUE = timedelta(hours=18)


def has_google_calendar_provider_state(connection):
    """Return whether a connection retains any durable provider-owned state."""

    return any(
        (
            connection.provider_account_id,
            connection.provider_email,
            connection.calendar_id,
            connection.calendar_operation_id,
            connection.access_token,
            connection.refresh_token,
            connection.sync_token,
            connection.page_token,
            connection.token_expires_at,
            connection.scopes,
            connection.credential_fingerprint,
        )
    )


def is_google_calendar_reconciliation_overdue(completed_at, at):
    """Return whether healthy reconciliation requires immediate dispatch."""

    return completed_at is None or completed_at <= at - GOOGLE_CALENDAR_HEALTHY_RECONCILIATION_OVERDUE
