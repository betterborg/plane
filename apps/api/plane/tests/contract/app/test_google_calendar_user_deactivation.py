# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import Mock, call, patch

import pytest
from rest_framework import status

from plane.bgtasks.google_calendar_task import (
    reconcile_google_calendar_connection,
    schedule_google_calendar_reconciliations,
)
from plane.db.models import GoogleCalendarConnection, Profile, WorkspaceMember
from plane.integrations.google_calendar.dispatch import GOOGLE_CALENDAR_LIFECYCLE_TASK
from plane.tests.factories import (
    GoogleCalendarConnectionFactory,
    WorkspaceIntegrationFactory,
    WorkspaceMemberFactory,
)


def _calendar_connection(member, *, generation, calendar_id=None):
    workspace_integration = WorkspaceIntegrationFactory(
        integration__title="Google Calendar",
        integration__provider="google_calendar",
        config={"enabled": True},
    )
    WorkspaceMemberFactory(
        workspace=workspace_integration.workspace,
        member=member,
        role=15,
    )
    if calendar_id is None:
        return GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            member=member,
            attempt_only=True,
            lifecycle_generation=generation,
        )
    return GoogleCalendarConnectionFactory(
        workspace_integration=workspace_integration,
        member=member,
        active=True,
        calendar_id=calendar_id,
        lifecycle_generation=generation,
        attempt_only=True,
    )


def _deactivation_connections(member):
    first = _calendar_connection(
        member,
        generation=3,
        calendar_id="first-dedicated-calendar",
    )
    second = _calendar_connection(
        member,
        generation=7,
        calendar_id="second-dedicated-calendar",
    )
    attempt = _calendar_connection(member, generation=11)
    return first, second, attempt


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarUserDeactivation:
    def test_deactivation_advances_all_connections_before_shared_cleanup(self, session_client, create_user):
        Profile.objects.create(user=create_user)
        first, second, attempt = _deactivation_connections(create_user)
        provider_connections = sorted((first, second), key=lambda connection: connection.id)
        published_states = []
        lifecycle_task = Mock()

        def cleanup(connection_id, generation):
            connection = GoogleCalendarConnection.objects.get(id=connection_id)
            published_states.append(
                (
                    connection_id,
                    generation,
                    connection.desired_state,
                    connection.status,
                    connection.lifecycle_generation,
                )
            )
            return reconcile_google_calendar_connection.run(connection_id, generation)

        lifecycle_task.delay.side_effect = cleanup
        provider_client = Mock(access_token=None)

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.app.views.user.base.current_app.signature",
                return_value=lifecycle_task,
            ) as signature,
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=provider_client),
            patch("plane.app.views.user.base.user_deactivation_email.delay"),
        ):
            response = session_client.delete("/api/users/me/")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        create_user.refresh_from_db()
        assert create_user.is_active is False
        assert not WorkspaceMember.objects.filter(member=create_user, is_active=True).exists()
        assert published_states == [
            (
                str(connection.id),
                connection.lifecycle_generation + 1,
                GoogleCalendarConnection.DesiredState.DISCONNECTED,
                GoogleCalendarConnection.Status.CLEANUP_PENDING,
                connection.lifecycle_generation + 1,
            )
            for connection in provider_connections
        ]
        assert signature.call_args_list == [
            call(GOOGLE_CALENDAR_LIFECYCLE_TASK) for _connection in provider_connections
        ]
        provider_client.delete_calendar.assert_has_calls(
            [call("first-dedicated-calendar"), call("second-dedicated-calendar")],
            any_order=True,
        )

        for connection in provider_connections:
            persisted = GoogleCalendarConnection.objects.get(id=connection.id)
            assert persisted.status == GoogleCalendarConnection.Status.DISCONNECTED
            assert persisted.lifecycle_generation == connection.lifecycle_generation + 1
            assert persisted.calendar_id == ""
            assert persisted.provider_account_id == ""
            assert persisted.refresh_token == ""

        persisted_attempt = GoogleCalendarConnection.objects.get(id=attempt.id)
        assert persisted_attempt.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert persisted_attempt.lifecycle_generation == 11
        assert persisted_attempt.oauth_state == ""
        assert persisted_attempt.oauth_code_verifier == ""
        assert persisted_attempt.oauth_redirect_uri == ""
        assert persisted_attempt.oauth_attempt_expires_at is None

    def test_broker_failure_preserves_deactivation_and_hourly_recovery(self, session_client, create_user):
        Profile.objects.create(user=create_user)
        first, second, attempt = _deactivation_connections(create_user)
        provider_connections = sorted((first, second), key=lambda connection: connection.id)
        failed_generations = []
        failed_task = Mock()

        def fail_publication(connection_id, generation):
            connection = GoogleCalendarConnection.objects.get(id=connection_id)
            failed_generations.append((connection_id, generation, connection.status))
            raise RuntimeError("lifecycle broker unavailable")

        failed_task.delay.side_effect = fail_publication
        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.app.views.user.base.current_app.signature", return_value=failed_task),
            patch("plane.app.views.user.base.user_deactivation_email.delay"),
        ):
            response = session_client.delete("/api/users/me/")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        create_user.refresh_from_db()
        assert create_user.is_active is False
        assert failed_generations == [
            (
                str(connection.id),
                connection.lifecycle_generation + 1,
                GoogleCalendarConnection.Status.CLEANUP_PENDING,
            )
            for connection in provider_connections
        ]

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

        assert published == len(provider_connections)
        assert recovered_generations == [
            (str(connection.id), connection.lifecycle_generation + 1) for connection in provider_connections
        ]
        assert signature.call_args_list == [
            call(GOOGLE_CALENDAR_LIFECYCLE_TASK) for _connection in provider_connections
        ]
        provider_client.delete_calendar.assert_has_calls(
            [call("first-dedicated-calendar"), call("second-dedicated-calendar")],
            any_order=True,
        )
        assert recovery_task.delay.call_count == len(provider_connections)

        persisted_attempt = GoogleCalendarConnection.objects.get(id=attempt.id)
        assert persisted_attempt.status == GoogleCalendarConnection.Status.DISCONNECTED
        assert persisted_attempt.lifecycle_generation == 11
        assert persisted_attempt.oauth_state == ""
        assert persisted_attempt.provider_account_id == ""
        assert persisted_attempt.calendar_id == ""
