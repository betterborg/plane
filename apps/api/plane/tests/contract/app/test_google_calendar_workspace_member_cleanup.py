# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import Mock, patch

import pytest

from plane.bgtasks.google_calendar_task import (
    reconcile_google_calendar_connection,
    schedule_google_calendar_reconciliations,
)
from plane.db.models import GoogleCalendarConnection, WorkspaceMember
from plane.integrations.google_calendar.dispatch import GOOGLE_CALENDAR_LIFECYCLE_TASK
from plane.tests.factories import (
    GoogleCalendarConnectionFactory,
    UserFactory,
    WorkspaceIntegrationFactory,
    WorkspaceMemberFactory,
)


def _active_member_connection():
    workspace_integration = WorkspaceIntegrationFactory(
        integration__title="Google Calendar",
        integration__provider="google_calendar",
        config={"enabled": True},
    )
    member = UserFactory()
    workspace_member = WorkspaceMemberFactory(
        workspace=workspace_integration.workspace,
        member=member,
        role=15,
    )
    connection = GoogleCalendarConnectionFactory(
        workspace_integration=workspace_integration,
        member=member,
        active=True,
        calendar_id="member-dedicated-calendar",
        lifecycle_generation=7,
        oauth_state="abandoned-attempt",
    )
    return workspace_member, connection


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarWorkspaceMemberCleanup:
    @pytest.mark.parametrize("mutation", ("deactivate", "remove"))
    def test_row_member_removal_deletes_the_dedicated_calendar(self, mutation):
        workspace_member, connection = _active_member_connection()
        lifecycle_task = Mock()
        lifecycle_task.delay.side_effect = reconcile_google_calendar_connection.run
        provider_client = Mock(access_token=None)

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.integrations.google_calendar.signals.current_app.signature",
                return_value=lifecycle_task,
            ) as signature,
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=provider_client),
            patch("plane.db.mixins.soft_delete_related_objects.delay"),
        ):
            if mutation == "deactivate":
                workspace_member.is_active = False
                workspace_member.save(update_fields=["is_active", "updated_at"])
            else:
                workspace_member.delete()

        connection.refresh_from_db()
        persisted_member = WorkspaceMember.all_objects.get(id=workspace_member.id)
        if mutation == "deactivate":
            assert persisted_member.is_active is False
        else:
            assert persisted_member.deleted_at is not None
        signature.assert_called_once_with(GOOGLE_CALENDAR_LIFECYCLE_TASK)
        lifecycle_task.delay.assert_called_once_with(str(connection.id), 8)
        provider_client.delete_calendar.assert_called_once_with("member-dedicated-calendar")
        assert connection.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        assert connection.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert connection.lifecycle_generation == 8
        assert connection.calendar_id == ""
        assert connection.provider_account_id == ""
        assert connection.refresh_token == ""

    def test_tokenless_attempt_is_invalidated_without_provider_work(self):
        workspace_integration = WorkspaceIntegrationFactory(
            integration__title="Google Calendar",
            integration__provider="google_calendar",
            config={"enabled": True},
        )
        member = UserFactory()
        workspace_member = WorkspaceMemberFactory(
            workspace=workspace_integration.workspace,
            member=member,
            role=15,
        )
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            member=member,
            attempt_only=True,
            lifecycle_generation=4,
        )

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.integrations.google_calendar.signals.current_app.signature") as signature,
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient") as provider_client,
        ):
            workspace_member.is_active = False
            workspace_member.save(update_fields=["is_active", "updated_at"])

        connection.refresh_from_db()
        signature.assert_not_called()
        provider_client.assert_not_called()
        assert connection.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        assert connection.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert connection.lifecycle_generation == 4
        assert connection.oauth_state == ""
        assert connection.oauth_code_verifier == ""
        assert connection.oauth_redirect_uri == ""
        assert connection.oauth_attempt_expires_at is None

    def test_broker_failure_leaves_cleanup_for_hourly_recovery(self):
        workspace_member, connection = _active_member_connection()
        failed_task = Mock()
        failed_task.delay.side_effect = RuntimeError("broker unavailable")

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.integrations.google_calendar.signals.current_app.signature",
                return_value=failed_task,
            ),
        ):
            workspace_member.is_active = False
            workspace_member.save(update_fields=["is_active", "updated_at"])

        workspace_member.refresh_from_db()
        connection.refresh_from_db()
        assert workspace_member.is_active is False
        assert connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert connection.lifecycle_generation == 8
        failed_task.delay.assert_called_once_with(str(connection.id), 8)

        recovered_generations = []
        recovery_task = Mock()

        def recover(connection_id, generation):
            recovered_generations.append((connection_id, generation))
            return reconcile_google_calendar_connection.run(connection_id, generation)

        recovery_task.delay.side_effect = recover
        provider_client = Mock(access_token=None)
        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.bgtasks.google_calendar_task.current_app.signature",
                return_value=recovery_task,
            ) as signature,
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=provider_client),
        ):
            published = schedule_google_calendar_reconciliations.run()

        connection.refresh_from_db()
        assert published == 1
        assert recovered_generations == [(str(connection.id), 8)]
        signature.assert_called_once_with(GOOGLE_CALENDAR_LIFECYCLE_TASK)
        provider_client.delete_calendar.assert_called_once_with("member-dedicated-calendar")
        assert connection.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert connection.provider_account_id == ""
