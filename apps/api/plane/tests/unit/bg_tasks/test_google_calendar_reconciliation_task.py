# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from plane.bgtasks.google_calendar_task import reconcile_google_calendar_inventory, synchronize_google_calendar_issue
from plane.db.models import GoogleCalendarEvent
from plane.integrations.google_calendar.client import GoogleCalendarEventPage, GoogleCalendarSyncTokenExpired
from plane.integrations.google_calendar.dispatch import GOOGLE_CALENDAR_INVENTORY_TASK, GOOGLE_CALENDAR_ISSUE_SYNC_TASK
from plane.tests.factories import (
    GoogleCalendarConnectionFactory,
    GoogleCalendarEventFactory,
    IssueAssigneeFactory,
    IssueFactory,
    WorkspaceIntegrationFactory,
)


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
        workspace_integration = WorkspaceIntegrationFactory(
            integration__provider="google_calendar",
            config={"enabled": True, "mode": "assignment"},
        )
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            active=True,
            sync_token="current-sync-token",
        )
        issue = IssueFactory(project__workspace=workspace_integration.workspace)
        IssueAssigneeFactory(issue=issue, assignee=connection.member, project=issue.project)
        correlation = GoogleCalendarEventFactory(
            connection=connection,
            entity_id=issue.id,
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
        client.get_event.return_value = {"id": "known-provider-id"}
        client.update_event.return_value = {"id": "known-provider-id", "status": "confirmed"}

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
        ):
            assert reconcile_google_calendar_inventory.run(str(connection.id)) == "continued"

            continuation = publish.call_args
            assert continuation.args[0].task == GOOGLE_CALENDAR_INVENTORY_TASK
            publish.reset_mock()

            def run_targeted_sync(task, entity_id, connection_id):
                assert task.task == GOOGLE_CALENDAR_ISSUE_SYNC_TASK
                synchronize_google_calendar_issue.run(entity_id, connection_id)

            publish.side_effect = run_targeted_sync
            assert reconcile_google_calendar_inventory.run(*continuation.args[1:]) == "complete"

        correlation.refresh_from_db()
        assert correlation.provider_status == "confirmed"
        assert correlation.provider_payload_hash == correlation.payload_hash
        assert GoogleCalendarEvent.objects.filter(connection=connection).count() == 1
        client.update_event.assert_called_once()

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
            "sync_token": "current-sync-token",
        }

    def test_local_scan_uses_bounded_keyset_pages_without_overlapping_pacing_windows(self):
        run_id = str(uuid4())
        connection = GoogleCalendarConnectionFactory(
            active=True,
            sync_token="current-sync-token",
            reconciliation_phase="local_scan",
            reconciliation_cursor=json.dumps(
                {
                    "run_id": run_id,
                    "calendar_generation": 1,
                    "lifecycle_generation": 1,
                    "after_id": "",
                    "force_local_scan": True,
                    "full_inventory": False,
                    "saw_delta": False,
                }
            ),
            reconciliation_lease_expires_at=None,
        )
        GoogleCalendarEvent.objects.bulk_create(
            [
                GoogleCalendarEvent(
                    connection=connection,
                    entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
                    entity_id=uuid4(),
                    google_event_id=f"provider-event-{index}",
                    payload_hash="0" * 64,
                    calendar_generation=connection.calendar_generation,
                )
                for index in range(1001)
            ]
        )

        with patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish:
            assert reconcile_google_calendar_inventory.run(str(connection.id), run_id) == "continued"

            first_page_calls = publish.call_args_list
            first_page_syncs = [
                call for call in first_page_calls if call.args[0].task == GOOGLE_CALENDAR_ISSUE_SYNC_TASK
            ]
            continuation = first_page_calls[-1]
            assert len(first_page_syncs) == 1000
            assert [call.args[0].options["countdown"] for call in first_page_syncs] == list(range(1000))
            assert continuation.args[0].task == GOOGLE_CALENDAR_INVENTORY_TASK
            assert continuation.args[0].options["countdown"] == 1000

            publish.reset_mock()
            assert reconcile_google_calendar_inventory.run(*continuation.args[1:]) == "complete"

        assert publish.call_count == 1
        assert publish.call_args.args[0].task == GOOGLE_CALENDAR_ISSUE_SYNC_TASK
        connection.refresh_from_db()
        assert connection.reconciliation_phase == ""
        assert connection.reconciliation_completed_at is not None

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
