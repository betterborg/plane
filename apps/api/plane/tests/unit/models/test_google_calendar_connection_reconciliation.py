# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import logging

import pytest
from django.db import connection as database_connection
from django.utils import timezone

from plane.app.serializers.integration import (
    GoogleCalendarConnectionRosterStatusSerializer,
    GoogleCalendarConnectionStatusSerializer,
)
from plane.tests.factories import GoogleCalendarConnectionFactory


@pytest.mark.unit
@pytest.mark.django_db
class TestGoogleCalendarConnectionReconciliationState:
    def test_private_reconciliation_state_persists_with_provider_tokens_encrypted(self):
        lease_expires_at = timezone.now()
        completed_at = timezone.now()
        connection = GoogleCalendarConnectionFactory(
            sync_token="private-sync-token",
            page_token="private-page-token",
            credential_fingerprint="a" * 64,
            reconciliation_lease_expires_at=lease_expires_at,
            reconciliation_completed_at=completed_at,
            reconciliation_phase="provider_inventory",
            reconciliation_cursor="private-ledger-cursor",
            calendar_generation=9,
        )

        table = connection._meta.db_table
        with database_connection.cursor() as cursor:
            cursor.execute(
                f'SELECT "sync_token", "page_token" FROM "{table}" WHERE "id" = %s',
                [connection.id],
            )
            stored_sync_token, stored_page_token = cursor.fetchone()

        assert stored_sync_token.startswith("fernet$")
        assert stored_page_token.startswith("fernet$")
        assert "private-sync-token" not in stored_sync_token
        assert "private-page-token" not in stored_page_token

        connection.refresh_from_db()
        assert connection.sync_token == "private-sync-token"
        assert connection.page_token == "private-page-token"
        assert connection.credential_fingerprint == "a" * 64
        assert connection.reconciliation_lease_expires_at == lease_expires_at
        assert connection.reconciliation_completed_at == completed_at
        assert connection.reconciliation_phase == "provider_inventory"
        assert connection.reconciliation_cursor == "private-ledger-cursor"
        assert connection.calendar_generation == 9

    def test_public_serializers_and_log_identity_omit_private_reconciliation_state(self, caplog):
        private_values = {
            "private-sync-token",
            "private-page-token",
            "private-ledger-cursor",
            "b" * 64,
        }
        connection = GoogleCalendarConnectionFactory(
            sync_token="private-sync-token",
            page_token="private-page-token",
            credential_fingerprint="b" * 64,
            reconciliation_cursor="private-ledger-cursor",
        )

        serialized = str(GoogleCalendarConnectionStatusSerializer(connection).data)
        serialized += str(GoogleCalendarConnectionRosterStatusSerializer(connection).data)
        private_field_names = {
            "sync_token",
            "page_token",
            "credential_fingerprint",
            "reconciliation_cursor",
        }
        assert private_field_names.isdisjoint(GoogleCalendarConnectionStatusSerializer().fields)
        assert private_field_names.isdisjoint(GoogleCalendarConnectionRosterStatusSerializer().fields)

        logger = logging.getLogger("plane.tests.google_calendar_reconciliation")
        with caplog.at_level(logging.INFO, logger=logger.name):
            logger.info("Calendar connection: %s", connection)

        for private_value in private_values:
            assert private_value not in serialized
            assert private_value not in caplog.text
