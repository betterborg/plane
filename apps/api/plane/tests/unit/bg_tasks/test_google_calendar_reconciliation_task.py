# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import pytest
from django.utils import timezone
from freezegun import freeze_time

from plane.bgtasks.google_calendar_task import (
    GOOGLE_CALENDAR_RECONCILIATION_MAX_STAGGER_SECONDS,
    _release_reconciliation_lease,
    _start_or_resume_inventory,
    reconcile_google_calendar_connection,
    reconcile_google_calendar_inventory,
    schedule_google_calendar_reconciliations,
    synchronize_google_calendar_issue,
)
from plane.db.models import GoogleCalendarConnection, GoogleCalendarEvent
from plane.integrations.google_calendar.client import GoogleCalendarEventPage, GoogleCalendarSyncTokenExpired
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_INVENTORY_TASK,
    GOOGLE_CALENDAR_ISSUE_SYNC_TASK,
    GOOGLE_CALENDAR_LIFECYCLE_TASK,
)
from plane.tests.factories import (
    GoogleCalendarConnectionFactory,
    GoogleCalendarEventFactory,
    IssueAssigneeFactory,
    IssueFactory,
    WorkspaceMemberFactory,
    WorkspaceIntegrationFactory,
)


def _provider_client(*pages):
    client = Mock()
    client.access_token = None
    client.list_event_page.side_effect = pages
    return client


def _enabled_calendar_integration():
    return WorkspaceIntegrationFactory(
        integration__provider="google_calendar",
        config={"enabled": True, "mode": "assignment"},
    )


@pytest.mark.unit
@pytest.mark.django_db
class TestScheduleGoogleCalendarReconciliations:
    def test_hourly_scheduler_entry_is_registered(self):
        from plane.celery import app

        entry = app.conf.beat_schedule["schedule-google-calendar-reconciliations"]
        assert entry["task"] == "plane.bgtasks.google_calendar_task.schedule_google_calendar_reconciliations"
        assert entry["schedule"].minute == {0}

    @freeze_time("2026-08-24 12:00:00")
    def test_selects_six_hour_due_connections_and_skips_active_two_hour_leases(self):
        at = timezone.now()
        workspace_integration = _enabled_calendar_integration()
        due = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            active=True,
            reconciliation_completed_at=at - timedelta(hours=6),
        )
        recent = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            active=True,
            reconciliation_completed_at=at - timedelta(hours=6) + timedelta(seconds=1),
        )
        leased = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            active=True,
            reconciliation_completed_at=at - timedelta(hours=7),
            reconciliation_lease_expires_at=at + timedelta(hours=2),
        )
        expired_lease = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            active=True,
            reconciliation_completed_at=at - timedelta(hours=7),
            reconciliation_lease_expires_at=at,
        )
        disabled = GoogleCalendarConnectionFactory(
            active=True,
            workspace_integration__config={"enabled": False},
            reconciliation_completed_at=at - timedelta(hours=7),
        )
        unhealthy = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            bound_broken=True,
            reconciliation_completed_at=at - timedelta(hours=7),
        )

        with patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish:
            assert schedule_google_calendar_reconciliations.run() == 2

        published_ids = {call.args[1] for call in publish.call_args_list}
        assert published_ids == {str(due.id), str(expired_lease.id)}
        assert str(recent.id) not in published_ids
        assert str(leased.id) not in published_ids
        assert str(disabled.id) not in published_ids
        assert str(unhealthy.id) not in published_ids

    @freeze_time("2026-08-24 12:00:00")
    def test_stagger_is_deterministic_capped_and_overdue_work_is_immediate(self):
        at = timezone.now()
        workspace_integration = _enabled_calendar_integration()
        staggered = GoogleCalendarConnectionFactory(
            id=UUID(int=GOOGLE_CALENDAR_RECONCILIATION_MAX_STAGGER_SECONDS),
            workspace_integration=workspace_integration,
            active=True,
            reconciliation_completed_at=at - timedelta(hours=6),
        )
        overdue = GoogleCalendarConnectionFactory(
            id=UUID(int=GOOGLE_CALENDAR_RECONCILIATION_MAX_STAGGER_SECONDS * 2 + 1),
            workspace_integration=workspace_integration,
            active=True,
            reconciliation_completed_at=at - timedelta(hours=18),
        )
        never_completed = GoogleCalendarConnectionFactory(
            id=UUID(int=GOOGLE_CALENDAR_RECONCILIATION_MAX_STAGGER_SECONDS * 3 + 2),
            workspace_integration=workspace_integration,
            active=True,
            reconciliation_completed_at=None,
        )

        with patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish:
            assert schedule_google_calendar_reconciliations.run() == 3

        countdowns = {call.args[1]: call.args[0].options["countdown"] for call in publish.call_args_list}
        assert countdowns[str(staggered.id)] == GOOGLE_CALENDAR_RECONCILIATION_MAX_STAGGER_SECONDS
        assert countdowns[str(overdue.id)] == 0
        assert countdowns[str(never_completed.id)] == 0

    @freeze_time("2026-08-24 12:00:00")
    def test_publication_failure_is_isolated_and_failed_connection_remains_due(self):
        at = timezone.now()
        workspace_integration = _enabled_calendar_integration()
        connections = [
            GoogleCalendarConnectionFactory(
                id=UUID(int=index),
                workspace_integration=workspace_integration,
                active=True,
                reconciliation_completed_at=at - timedelta(hours=7),
            )
            for index in range(1, 4)
        ]

        def publish_or_fail(task, connection_id):
            if connection_id == str(connections[0].id):
                raise RuntimeError("broker unavailable")

        with patch(
            "plane.bgtasks.google_calendar_task.publish_google_calendar_task",
            side_effect=publish_or_fail,
        ) as publish:
            assert schedule_google_calendar_reconciliations.run() == 2

        assert [call.args[1] for call in publish.call_args_list] == [str(connection.id) for connection in connections]
        for connection in connections:
            connection.refresh_from_db()
        assert connections[0].reconciliation_completed_at == at - timedelta(hours=7)
        assert connections[0].reconciliation_lease_expires_at is None
        assert connections[1].reconciliation_lease_expires_at == at + timedelta(hours=2)
        assert connections[2].reconciliation_lease_expires_at == at + timedelta(hours=2)

    @freeze_time("2026-08-24 12:00:00")
    def test_recovers_dropped_present_publications_for_the_exact_generations(self):
        workspace_integration = _enabled_calendar_integration()
        callback_connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="callback-account",
            refresh_token="callback-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            lifecycle_generation=7,
        )
        reenabled_connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="reenabled-account",
            refresh_token="reenabled-refresh-token",
            calendar_id="recorded-calendar",
            calendar_generation=4,
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            lifecycle_generation=11,
        )
        for connection in (callback_connection, reenabled_connection):
            WorkspaceMemberFactory(
                workspace=workspace_integration.workspace,
                member=connection.member,
            )

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
        ):
            assert schedule_google_calendar_reconciliations.run() == 2

        published = {call.args[1]: (call.args[0].task, call.args[2]) for call in publish.call_args_list}
        assert published == {
            str(callback_connection.id): (GOOGLE_CALENDAR_LIFECYCLE_TASK, 7),
            str(reenabled_connection.id): (GOOGLE_CALENDAR_LIFECYCLE_TASK, 11),
        }

        provision_client = Mock(access_token=None)
        provision_client.find_calendar.return_value = None
        provision_client.create_calendar.return_value = "provisioned-calendar"
        resume_client = Mock(access_token=None)
        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.bgtasks.google_calendar_task.GoogleCalendarClient",
                side_effect=[provision_client, resume_client],
            ),
            patch("plane.bgtasks.google_calendar_task.enqueue_google_calendar_task_on_commit") as enqueue,
        ):
            assert reconcile_google_calendar_connection.run(str(callback_connection.id), 7) == "active"
            assert reconcile_google_calendar_connection.run(str(reenabled_connection.id), 11) == "active"

        callback_connection.refresh_from_db()
        reenabled_connection.refresh_from_db()
        assert callback_connection.calendar_id == "provisioned-calendar"
        assert callback_connection.status == GoogleCalendarConnection.Status.ACTIVE
        assert reenabled_connection.calendar_id == "recorded-calendar"
        assert reenabled_connection.calendar_generation == 4
        assert reenabled_connection.status == GoogleCalendarConnection.Status.ACTIVE
        provision_client.create_calendar.assert_called_once()
        resume_client.get_calendar.assert_called_once_with("recorded-calendar")
        resume_client.create_calendar.assert_not_called()
        assert any(
            call.args[0] == reconcile_google_calendar_inventory
            and call.args[1] == str(reenabled_connection.id)
            and call.kwargs == {"force_local_scan": True}
            for call in enqueue.call_args_list
        )

    @freeze_time("2026-08-24 12:00:00")
    def test_present_recovery_excludes_ineligible_or_unusable_rows(self):
        enabled_integration = _enabled_calendar_integration()

        def pending_connection(*, workspace_integration=enabled_integration, active_member=True, **kwargs):
            connection = GoogleCalendarConnectionFactory(
                workspace_integration=workspace_integration,
                provider_account_id="google-account",
                refresh_token="refresh-token",
                desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
                status=GoogleCalendarConnection.Status.PENDING,
                lifecycle_generation=3,
                **kwargs,
            )
            WorkspaceMemberFactory(
                workspace=workspace_integration.workspace,
                member=connection.member,
                is_active=active_member,
            )
            return connection

        eligible = pending_connection()
        tokenless = pending_connection(refresh_token="", access_token="", token_expires_at=None)
        disabled = pending_connection(
            workspace_integration=WorkspaceIntegrationFactory(
                integration__provider="google_calendar",
                config={"enabled": False},
            )
        )
        inactive_member = pending_connection(active_member=False)
        terminal_tombstone = GoogleCalendarConnectionFactory(
            workspace_integration=enabled_integration,
            provider_account_id="retained-account",
            refresh_token="retained-token",
            tombstone=True,
        )
        WorkspaceMemberFactory(
            workspace=enabled_integration.workspace,
            member=terminal_tombstone.member,
        )
        soft_deleted = pending_connection()
        soft_deleted.deleted_at = timezone.now()
        soft_deleted.save(update_fields=["deleted_at", "updated_at"])

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient") as client_class,
        ):
            assert schedule_google_calendar_reconciliations.run() == 1

        publish.assert_called_once()
        assert publish.call_args.args[0].task == GOOGLE_CALENDAR_LIFECYCLE_TASK
        assert publish.call_args.args[1:] == (str(eligible.id), 3)
        client_class.assert_not_called()
        excluded_ids = {
            str(tokenless.id),
            str(disabled.id),
            str(inactive_member.id),
            str(terminal_tombstone.id),
            str(soft_deleted.id),
        }
        assert not excluded_ids.intersection(call.args[1] for call in publish.call_args_list)

    def test_present_recovery_rechecks_the_grant_under_the_connection_lock(self):
        with freeze_time("2026-08-24 12:00:00") as frozen_time:
            workspace_integration = _enabled_calendar_integration()
            connection = GoogleCalendarConnectionFactory(
                workspace_integration=workspace_integration,
                provider_account_id="google-account",
                refresh_token="",
                access_token="access-token",
                token_expires_at=timezone.now() + timedelta(minutes=1),
                desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
                status=GoogleCalendarConnection.Status.PENDING,
                lifecycle_generation=5,
            )
            WorkspaceMemberFactory(
                workspace=workspace_integration.workspace,
                member=connection.member,
            )

            with (
                patch(
                    "plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock",
                    side_effect=lambda _lock_key: frozen_time.tick(delta=timedelta(minutes=2)),
                ) as acquire_lock,
                patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
            ):
                assert schedule_google_calendar_reconciliations.run() == 0

        assert acquire_lock.call_count == 2
        publish.assert_not_called()


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

    def test_inventory_provider_id_ownership_wins_over_a_copied_marker(self):
        connection = GoogleCalendarConnectionFactory(active=True, sync_token="current-sync-token")
        correlation = GoogleCalendarEventFactory(
            connection=connection,
            google_event_id="ledger-owned-provider-id",
            provider_status="confirmed",
        )
        marker = {
            "private": {
                "plane_entity_type": correlation.entity_type,
                "plane_entity_id": str(correlation.entity_id),
            }
        }
        page = GoogleCalendarEventPage(
            (
                {
                    "id": "ledger-owned-provider-id",
                    "status": "confirmed",
                    "etag": '"owned"',
                    "extendedProperties": marker,
                },
                {
                    "id": "foreign-copied-marker",
                    "status": "confirmed",
                    "etag": '"foreign"',
                    "extendedProperties": marker,
                },
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
        assert correlation.google_event_id == "ledger-owned-provider-id"
        assert correlation.provider_etag == '"owned"'

    def test_inventory_provider_id_ownership_survives_pages_and_worker_continuations(self):
        workspace_integration = WorkspaceIntegrationFactory(
            integration__provider="google_calendar",
            config={"enabled": True, "mode": "assignment"},
        )
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            active=True,
            sync_token="",
        )
        issue = IssueFactory(project__workspace=workspace_integration.workspace)
        IssueAssigneeFactory(issue=issue, assignee=connection.member, project=issue.project)
        correlation = GoogleCalendarEventFactory(
            connection=connection,
            entity_id=issue.id,
            google_event_id="ledger-owned-provider-id",
            provider_status="confirmed",
        )
        marker = {
            "private": {
                "plane_entity_type": correlation.entity_type,
                "plane_entity_id": str(correlation.entity_id),
            }
        }
        pages = [
            GoogleCalendarEventPage(
                (
                    {
                        "id": "ledger-owned-provider-id",
                        "status": "confirmed",
                        "etag": '"owned"',
                        "extendedProperties": marker,
                    },
                ),
                "page-1",
                None,
            ),
            GoogleCalendarEventPage((), "page-2", None),
            GoogleCalendarEventPage((), "page-3", None),
            GoogleCalendarEventPage((), "page-4", None),
            GoogleCalendarEventPage((), "page-5", None),
            GoogleCalendarEventPage(
                (
                    {
                        "id": "foreign-copied-marker",
                        "status": "confirmed",
                        "etag": '"foreign"',
                        "extendedProperties": marker,
                    },
                ),
                None,
                "next-sync-token",
            ),
        ]
        client = _provider_client(*pages)
        client.get_event.return_value = {"id": "ledger-owned-provider-id"}
        client.update_event.return_value = {"id": "ledger-owned-provider-id", "status": "confirmed"}

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
        ):
            assert reconcile_google_calendar_inventory.run(str(connection.id)) == "continued"
            provider_continuation = publish.call_args

            publish.reset_mock()
            assert reconcile_google_calendar_inventory.run(*provider_continuation.args[1:]) == "continued"
            local_continuation = publish.call_args

            def run_targeted_sync(task, entity_id, connection_id):
                assert task.task == GOOGLE_CALENDAR_ISSUE_SYNC_TASK
                synchronize_google_calendar_issue.run(entity_id, connection_id)

            publish.reset_mock()
            publish.side_effect = run_targeted_sync
            assert reconcile_google_calendar_inventory.run(*local_continuation.args[1:]) == "complete"

        correlation.refresh_from_db()
        assert correlation.google_event_id == "ledger-owned-provider-id"
        assert correlation.provider_etag != '"foreign"'
        assert client.update_event.call_args.args[1] == "ledger-owned-provider-id"

    def test_full_inventory_cancelled_id_ownership_wins_across_pages(self):
        connection = GoogleCalendarConnectionFactory(active=True, sync_token="")
        correlation = GoogleCalendarEventFactory(
            connection=connection,
            google_event_id="ledger-owned-cancelled-id",
        )
        marker = {
            "private": {
                "plane_entity_type": correlation.entity_type,
                "plane_entity_id": str(correlation.entity_id),
            }
        }
        client = _provider_client(
            GoogleCalendarEventPage(
                ({"id": "ledger-owned-cancelled-id", "status": "cancelled"},),
                "page-1",
                None,
            ),
            GoogleCalendarEventPage(
                (
                    {
                        "id": "foreign-copied-marker",
                        "status": "confirmed",
                        "extendedProperties": marker,
                    },
                ),
                None,
                "next-sync-token",
            ),
        )

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task"),
        ):
            assert reconcile_google_calendar_inventory.run(str(connection.id)) == "continued"

        correlation.refresh_from_db()
        assert correlation.google_event_id == "ledger-owned-cancelled-id"
        assert correlation.provider_status == "cancelled"

    def test_full_inventory_recovers_an_exact_marker_only_after_the_final_page(self):
        connection = GoogleCalendarConnectionFactory(active=True, sync_token="")
        correlation = GoogleCalendarEventFactory(
            connection=connection,
            google_event_id="absent-ledger-provider-id",
        )
        marker = {
            "private": {
                "plane_entity_type": correlation.entity_type,
                "plane_entity_id": str(correlation.entity_id),
            }
        }
        client = _provider_client(
            GoogleCalendarEventPage(
                (
                    {
                        "id": "recovered-exact-marker",
                        "status": "confirmed",
                        "etag": '"recovered"',
                        "extendedProperties": marker,
                    },
                ),
                None,
                "next-sync-token",
            )
        )

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task"),
        ):
            assert reconcile_google_calendar_inventory.run(str(connection.id)) == "continued"

        correlation.refresh_from_db()
        assert correlation.google_event_id == "recovered-exact-marker"
        assert correlation.provider_etag == '"recovered"'

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

    def test_prior_lifecycle_lease_cannot_block_forced_reconnect_scan(self):
        stale_run_id = str(uuid4())
        connection = GoogleCalendarConnectionFactory(
            active=True,
            lifecycle_generation=2,
            sync_token="current-sync-token",
            reconciliation_phase="provider_inventory",
            reconciliation_cursor=json.dumps(
                {
                    "run_id": stale_run_id,
                    "lease_token": str(uuid4()),
                    "calendar_generation": 1,
                    "lifecycle_generation": 1,
                    "after_id": "",
                    "force_local_scan": False,
                    "full_inventory": False,
                    "saw_delta": False,
                }
            ),
            reconciliation_lease_expires_at=timezone.now() + timedelta(minutes=5),
        )
        correlation = GoogleCalendarEventFactory(connection=connection)
        client = _provider_client(GoogleCalendarEventPage((), None, "next-token"))

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
        ):
            assert (
                reconcile_google_calendar_inventory.run(
                    str(connection.id),
                    force_local_scan=True,
                )
                == "continued"
            )

        connection.refresh_from_db()
        current_state = json.loads(connection.reconciliation_cursor)
        assert current_state["run_id"] != stale_run_id
        assert current_state["lifecycle_generation"] == connection.lifecycle_generation
        assert current_state["force_local_scan"] is True
        assert connection.reconciliation_phase == "local_scan"
        client.list_event_page.assert_called_once_with(
            connection.calendar_id,
            page_token=None,
            sync_token="current-sync-token",
        )
        assert publish.call_args.args[0].task == GOOGLE_CALENDAR_INVENTORY_TASK
        assert reconcile_google_calendar_inventory.run(str(connection.id), stale_run_id) == "stale"
        assert GoogleCalendarEvent.objects.filter(id=correlation.id).exists()

    def test_expired_lease_holder_cannot_advance_or_release_takeover_lease(self):
        connection = GoogleCalendarConnectionFactory(active=True, sync_token="current-sync-token")
        page = GoogleCalendarEventPage((), None, "stale-worker-sync-token")
        takeover = {}

        def take_over_during_provider_request(*args, **kwargs):
            connection.refresh_from_db()
            original_state = json.loads(connection.reconciliation_cursor)
            takeover["run_id"] = original_state["run_id"]
            takeover["expired_lease_token"] = original_state["lease_token"]
            connection.reconciliation_lease_expires_at = timezone.now() - timedelta(seconds=1)
            connection.save(update_fields=["reconciliation_lease_expires_at", "updated_at"])

            claimed_connection = _start_or_resume_inventory(connection.id, original_state["run_id"], False)
            takeover["lease_token"] = json.loads(claimed_connection.reconciliation_cursor)["lease_token"]
            return page

        client = _provider_client()
        client.list_event_page.side_effect = take_over_during_provider_request

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
        ):
            result = reconcile_google_calendar_inventory.run(str(connection.id))

        assert result == "stale"
        assert takeover["lease_token"] != takeover["expired_lease_token"]
        assert not _release_reconciliation_lease(
            connection.id,
            takeover["run_id"],
            takeover["expired_lease_token"],
        )
        publish.assert_not_called()

        connection.refresh_from_db()
        current_state = json.loads(connection.reconciliation_cursor)
        assert current_state["lease_token"] == takeover["lease_token"]
        assert connection.reconciliation_lease_expires_at > timezone.now()
        assert connection.sync_token == "current-sync-token"

    def test_expired_lease_holder_cannot_advance_without_a_takeover(self):
        connection = GoogleCalendarConnectionFactory(active=True, sync_token="current-sync-token")
        page = GoogleCalendarEventPage((), None, "stale-worker-sync-token")

        def expire_lease_during_provider_request(*args, **kwargs):
            GoogleCalendarConnection.objects.filter(id=connection.id).update(
                reconciliation_lease_expires_at=timezone.now() - timedelta(seconds=1)
            )
            return page

        client = _provider_client()
        client.list_event_page.side_effect = expire_lease_during_provider_request

        with (
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=client),
            patch("plane.bgtasks.google_calendar_task.publish_google_calendar_task") as publish,
        ):
            assert reconcile_google_calendar_inventory.run(str(connection.id)) == "stale"

        connection.refresh_from_db()
        assert connection.sync_token == "current-sync-token"
        assert connection.reconciliation_completed_at is None
        publish.assert_not_called()
