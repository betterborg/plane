# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from plane.bgtasks.google_calendar_task import reconcile_google_calendar_inventory
from plane.db.models import GoogleCalendarEvent
from plane.integrations.google_calendar.client import GoogleCalendarEventPage, GoogleCalendarSyncTokenExpired
from plane.tests.factories import GoogleCalendarConnectionFactory, GoogleCalendarEventFactory


def _provider_client(*pages):
    client = Mock()
    client.access_token = None
    client.list_event_page.side_effect = pages
    return client


@pytest.mark.unit
@pytest.mark.django_db
class TestGoogleCalendarReconciliationTask:
    def test_no_delta_incremental_inventory_is_one_request_and_no_event_work(self):
        connection = GoogleCalendarConnectionFactory(active=True, sync_token="current-sync-token")
        GoogleCalendarEventFactory(connection=connection)
        client = _provider_client(GoogleCalendarEventPage((), None, "next-sync-token"))

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
        ):
            result = reconcile_google_calendar_inventory.run(str(connection.id))

        assert result == "complete"
        client.list_event_page.assert_called_once_with(
            connection.calendar_id,
            page_token=None,
            sync_token="current-sync-token",
        )
        publish.assert_not_called()
        connection.refresh_from_db()
        assert connection.sync_token == "next-sync-token"
        assert connection.reconciliation_completed_at is not None
        assert connection.reconciliation_phase == ""

    def test_inventory_resolves_id_only_tombstone_by_current_generation_ledger(self):
        connection = GoogleCalendarConnectionFactory(active=True, sync_token="current-sync-token")
        correlation = GoogleCalendarEventFactory(
            connection=connection,
            google_event_id="known-provider-id",
            provider_status="confirmed",
        )
        page = GoogleCalendarEventPage(
            (
                {"id": "known-provider-id", "status": "cancelled", "etag": '"deleted"'},
                {
                    "id": "unknown-provider-id",
                    "status": "confirmed",
                    "extendedProperties": {
                        "private": {
                            "plane_entity_type": GoogleCalendarEvent.EntityType.WORK_ITEM,
                            "plane_entity_id": str(uuid4()),
                        }
                    },
                },
                {"id": "unmarked-provider-id", "status": "confirmed"},
            ),
            None,
            "next-sync-token",
        )
        client = _provider_client(page)

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task"),
        ):
            assert reconcile_google_calendar_inventory.run(str(connection.id)) == "continued"

        correlation.refresh_from_db()
        assert correlation.provider_status == "cancelled"
        assert correlation.provider_etag == '"deleted"'
        assert correlation.provider_payload_hash == ""
        assert GoogleCalendarEvent.objects.filter(connection=connection).count() == 1

    def test_provider_invocation_stops_after_five_pages_and_persists_continuation_first(self):
        connection = GoogleCalendarConnectionFactory(active=True, sync_token="current-sync-token")
        pages = [GoogleCalendarEventPage((), f"page-{index}", None) for index in range(1, 6)]
        client = _provider_client(*pages)

        def assert_persisted_before_publish(task, connection_id, run_id):
            connection.refresh_from_db()
            assert connection.page_token == "page-5"
            assert json.loads(connection.reconciliation_cursor)["run_id"] == run_id

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch(
                "plane.bgtasks.google_calendar_task.publish_google_calendar_task",
                side_effect=assert_persisted_before_publish,
            ) as publish,
        ):
            assert reconcile_google_calendar_inventory.run(str(connection.id)) == "continued"

        assert client.list_event_page.call_count == 5
        publish.assert_called_once()
        assert client.list_event_page.call_args_list[1].kwargs == {
            "page_token": "page-1",
            "sync_token": None,
        }

    def test_expired_list_token_starts_full_inventory_without_touching_unknown_events(self):
        connection = GoogleCalendarConnectionFactory(active=True, sync_token="expired-sync-token")
        correlation = GoogleCalendarEventFactory(
            connection=connection,
            provider_etag='"old"',
            provider_payload_hash="0" * 64,
            provider_status="confirmed",
        )
        full_page = GoogleCalendarEventPage(
            ({"id": "unknown-provider-id", "status": "confirmed"},),
            None,
            "replacement-sync-token",
        )
        client = _provider_client(GoogleCalendarSyncTokenExpired("expired"), full_page)

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task"),
        ):
            assert reconcile_google_calendar_inventory.run(str(connection.id)) == "continued"

        assert client.list_event_page.call_args_list[0].kwargs["sync_token"] == "expired-sync-token"
        assert client.list_event_page.call_args_list[1].kwargs == {
            "page_token": None,
            "sync_token": None,
        }
        correlation.refresh_from_db()
        assert correlation.provider_etag == ""
        assert correlation.provider_payload_hash == ""
        assert correlation.provider_status == ""
        assert GoogleCalendarEvent.objects.filter(connection=connection).count() == 1

    def test_failed_continuation_publication_cannot_record_completion(self):
        connection = GoogleCalendarConnectionFactory(active=True, sync_token="current-sync-token")
        page = GoogleCalendarEventPage(({"id": "foreign", "status": "confirmed"},), None, "next-token")
        client = _provider_client(page)

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch(
                "plane.bgtasks.google_calendar_task.publish_google_calendar_task",
                side_effect=RuntimeError("broker unavailable"),
            ),
            pytest.raises(RuntimeError, match="broker unavailable"),
        ):
            reconcile_google_calendar_inventory.run(str(connection.id))

        connection.refresh_from_db()
        assert connection.reconciliation_phase == "local_scan"
        assert connection.reconciliation_completed_at is None

    def test_stale_run_id_cannot_advance_durable_state(self):
        connection = GoogleCalendarConnectionFactory(active=True, sync_token="current-sync-token")
        client = _provider_client(GoogleCalendarEventPage((), None, "next-token"))

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
        ):
            result = reconcile_google_calendar_inventory.run(str(connection.id), str(uuid4()))

        assert result == "stale"
        client.list_event_page.assert_not_called()
        publish.assert_not_called()
