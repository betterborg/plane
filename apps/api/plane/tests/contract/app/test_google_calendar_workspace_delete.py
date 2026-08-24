# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import Mock, call, patch
from uuid import UUID

import pytest
from django.utils import timezone
from rest_framework import status

from plane.bgtasks.google_calendar_task import _cleanup_recovery_candidates
from plane.db.models import GoogleCalendarConnection, Profile, Workspace
from plane.integrations.google_calendar.dispatch import GOOGLE_CALENDAR_LIFECYCLE_TASK
from plane.integrations.google_calendar.lifecycle import _transition
from plane.tests.factories import GoogleCalendarConnectionFactory, WorkspaceIntegrationFactory
from plane.utils.analytics_events import WORKSPACE_DELETED


def _workspace_url(workspace):
    return f"/api/workspaces/{workspace.slug}/"


def _provider_connection(workspace_integration, connection_id, generation):
    return GoogleCalendarConnectionFactory(
        id=connection_id,
        workspace_integration=workspace_integration,
        provider_account_id=f"account-{generation}",
        refresh_token=f"refresh-token-{generation}",
        desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
        status=GoogleCalendarConnection.Status.ACTIVE,
        lifecycle_generation=generation,
        attempt_only=True,
    )


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarWorkspaceDelete:
    def test_delete_atomically_invalidates_attempts_and_advances_every_provider_generation(
        self,
        session_client,
        workspace,
        create_user,
    ):
        workspace_integration = WorkspaceIntegrationFactory(workspace=workspace)
        healthy = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            active=True,
            lifecycle_generation=1,
            attempt_only=True,
        )
        broken = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            bound_broken=True,
            lifecycle_generation=2,
            refresh_token="broken-refresh-token",
            attempt_only=True,
        )
        provisioning = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="provisioning-account",
            refresh_token="provisioning-refresh-token",
            calendar_operation_id=UUID("12345678-1234-5678-1234-567812345678"),
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.PENDING,
            lifecycle_generation=3,
            attempt_only=True,
        )
        absent = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="absent-account",
            calendar_id="calendar-awaiting-delete",
            refresh_token="absent-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=4,
            retain_grant_after_cleanup=True,
            attempt_only=True,
        )
        GoogleCalendarConnection.all_objects.filter(id=absent.id).update(deleted_at=timezone.now())
        terminal = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="terminal-account",
            refresh_token="terminal-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.DISCONNECTED,
            lifecycle_generation=5,
            attempt_only=True,
        )
        attempt = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            attempt_only=True,
            lifecycle_generation=6,
        )
        Profile.objects.create(user=create_user, last_workspace_id=workspace.id)
        original_slug = workspace.slug

        lifecycle_task = Mock()
        callback_order = []

        def fail_lifecycle(*args, **kwargs):
            callback_order.append("lifecycle")
            raise RuntimeError("lifecycle broker unavailable")

        def fail_recursive_delete(*args, **kwargs):
            callback_order.append("recursive-delete")
            raise RuntimeError("recursive-delete broker unavailable")

        def fail_analytics(*args, **kwargs):
            callback_order.append("analytics")
            raise RuntimeError("analytics broker unavailable")

        lifecycle_task.delay.side_effect = fail_lifecycle
        provider_connections = sorted(
            (healthy, broken, provisioning, absent, terminal),
            key=lambda connection: connection.id,
        )

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.app.views.workspace.base.current_app.signature", return_value=lifecycle_task) as signature,
            patch(
                "plane.db.mixins.soft_delete_related_objects.delay",
                side_effect=fail_recursive_delete,
            ) as recursive_delete,
            patch(
                "plane.app.views.workspace.base.track_event.delay",
                side_effect=fail_analytics,
            ) as analytics,
        ):
            response = session_client.delete(_workspace_url(workspace))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        deleted_workspace = Workspace.all_objects.get(id=workspace.id)
        assert deleted_workspace.deleted_at is not None
        assert deleted_workspace.slug.startswith(f"{original_slug}__")
        assert Profile.objects.get(user=create_user).last_workspace_id is None

        for connection in provider_connections:
            persisted = GoogleCalendarConnection.all_objects.get(id=connection.id)
            assert persisted.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
            assert persisted.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
            assert persisted.lifecycle_generation == connection.lifecycle_generation + 1
            assert persisted.retain_grant_after_cleanup is False
            assert persisted.oauth_state == ""
            assert persisted.oauth_code_verifier == ""
            assert persisted.oauth_redirect_uri == ""
            assert persisted.oauth_attempt_expires_at is None

        persisted_attempt = GoogleCalendarConnection.all_objects.get(id=attempt.id)
        assert persisted_attempt.desired_state == GoogleCalendarConnection.DesiredState.DISCONNECTED
        assert persisted_attempt.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert persisted_attempt.lifecycle_generation == 6
        assert persisted_attempt.oauth_state == ""
        assert persisted_attempt.oauth_attempt_expires_at is None

        assert signature.call_count == len(provider_connections)
        assert all(publication == call(GOOGLE_CALENDAR_LIFECYCLE_TASK) for publication in signature.call_args_list)
        assert lifecycle_task.delay.call_args_list == [
            call(str(connection.id), connection.lifecycle_generation + 1) for connection in provider_connections
        ]
        recursive_delete.assert_called_once_with("db", "workspace", workspace.id, using=None)
        analytics.assert_called_once()
        assert analytics.call_args.kwargs["event_name"] == WORKSPACE_DELETED
        assert analytics.call_args.kwargs["slug"] == original_slug
        assert callback_order == [
            *(["lifecycle"] * len(provider_connections)),
            "recursive-delete",
            "analytics",
        ]
        assert set(_cleanup_recovery_candidates()) >= {connection.id for connection in provider_connections}

    def test_second_ordered_transition_failure_rolls_back_the_complete_delete(
        self,
        session_client,
        workspace,
        create_user,
    ):
        workspace_integration = WorkspaceIntegrationFactory(workspace=workspace)
        first = _provider_connection(
            workspace_integration,
            UUID("00000000-0000-0000-0000-000000000001"),
            1,
        )
        second = _provider_connection(
            workspace_integration,
            UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            2,
        )
        profile = Profile.objects.create(user=create_user, last_workspace_id=workspace.id)
        original_slug = workspace.slug
        transition_count = 0

        def fail_second_transition(*args, **kwargs):
            nonlocal transition_count
            transition_count += 1
            if transition_count == 2:
                raise RuntimeError("second transition failed")
            return _transition(*args, **kwargs)

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.integrations.google_calendar.lifecycle._transition",
                side_effect=fail_second_transition,
            ),
            patch("plane.app.views.workspace.base.current_app.signature") as signature,
            patch("plane.db.mixins.soft_delete_related_objects.delay") as recursive_delete,
            patch("plane.app.views.workspace.base.track_event.delay") as analytics,
        ):
            response = session_client.delete(_workspace_url(workspace))

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        persisted_workspace = Workspace.all_objects.get(id=workspace.id)
        assert persisted_workspace.deleted_at is None
        assert persisted_workspace.slug == original_slug
        profile.refresh_from_db()
        assert profile.last_workspace_id == workspace.id
        for connection in (first, second):
            persisted = GoogleCalendarConnection.objects.get(id=connection.id)
            assert persisted.desired_state == GoogleCalendarConnection.DesiredState.CONNECTED
            assert persisted.status == GoogleCalendarConnection.Status.ACTIVE
            assert persisted.lifecycle_generation == connection.lifecycle_generation
            assert persisted.oauth_state == connection.oauth_state
        signature.assert_not_called()
        recursive_delete.assert_not_called()
        analytics.assert_not_called()
