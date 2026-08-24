# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
import logging
import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from plane.bgtasks.google_calendar_task import (
    _synchronize_issue_for_connection,
    reconcile_google_calendar_connection,
)
from plane.db.models import GoogleCalendarConnection
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_LIFECYCLE_TASK,
    publish_google_calendar_task,
)
from plane.integrations.google_calendar.oauth import GoogleCalendarOAuthCredentials
from plane.integrations.google_calendar.telemetry import (
    log_google_calendar_operation,
    publish_google_calendar_analytics,
)
from plane.license.models import Instance, InstanceAdmin
from plane.license.utils.google_calendar_credentials import google_calendar_credential_fingerprint
from plane.tests.factories import GoogleCalendarConnectionFactory, WorkspaceIntegrationFactory


OAUTH_CREDENTIALS = GoogleCalendarOAuthCredentials("readiness-client", "readiness-secret")


@pytest.fixture
def calendar_instance_admin(create_user):
    instance = Instance.objects.create(
        instance_name="Calendar readiness instance",
        instance_id=str(uuid.uuid4()),
        current_version="1.0.0",
        domain="http://testserver",
        last_checked_at=timezone.now(),
        is_setup_done=True,
    )
    InstanceAdmin.objects.create(instance=instance, user=create_user, role=20)
    return create_user


def _verified_connection(**overrides):
    workspace_integration = WorkspaceIntegrationFactory(
        integration__provider="google_calendar",
        config={"enabled": True, "mode": "assignment"},
    )
    values = {
        "workspace_integration": workspace_integration,
        "active": True,
        "credential_fingerprint": google_calendar_credential_fingerprint(
            OAUTH_CREDENTIALS.client_id,
            OAUTH_CREDENTIALS.client_secret,
        ),
        "reconciliation_completed_at": timezone.now() - timedelta(hours=1),
    }
    values.update(overrides)
    return GoogleCalendarConnectionFactory(**values)


@pytest.mark.contract
@pytest.mark.django_db
class TestGoogleCalendarReleaseReadiness:
    def test_complete_backend_verification_is_ready_and_secret_free(self, session_client, calendar_instance_admin):
        connection = _verified_connection(
            provider_account_id="forbidden-provider-account",
            provider_email="forbidden-provider@example.com",
            access_token="forbidden-access-token",
            refresh_token="forbidden-refresh-token",
        )

        with patch(
            "plane.app.views.integration.get_google_calendar_oauth_credentials",
            return_value=OAUTH_CREDENTIALS,
        ):
            response = session_client.get(reverse("google-calendar-release-readiness"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["ready"] is True
        assert response.data["credential_binding_complete"] is True
        assert response.data["lifecycle_recovery_complete"] is True
        assert response.data["backend_verification_complete"] is True
        assert 3600 <= response.data["last_completion_age_seconds"] < 3605
        serialized = json.dumps(response.data)
        for forbidden in (
            "readiness-client",
            "readiness-secret",
            connection.credential_fingerprint,
            "forbidden-provider-account",
            "forbidden-provider@example.com",
            "forbidden-access-token",
            "forbidden-refresh-token",
        ):
            assert forbidden not in serialized

    @pytest.mark.parametrize(
        ("connection_values", "failed_contract"),
        [
            ({"credential_fingerprint": "mismatched-fingerprint"}, "credential_binding_complete"),
            ({"reconciliation_completed_at": timezone.now() - timedelta(hours=19)}, "reconciliation_overdue"),
            ({"reconciliation_completed_at": None}, "backend_verification_complete"),
            (
                {
                    "active": False,
                    "desired_state": "connected",
                    "status": "pending",
                    "reconciliation_completed_at": None,
                },
                "lifecycle_recovery_complete",
            ),
            (
                {
                    "active": False,
                    "desired_state": GoogleCalendarConnection.DesiredState.CONNECTED,
                    "status": GoogleCalendarConnection.Status.ERROR,
                    "reconciliation_completed_at": None,
                },
                "lifecycle_recovery_complete",
            ),
            (
                {
                    "active": False,
                    "desired_state": GoogleCalendarConnection.DesiredState.DISCONNECTED,
                    "status": GoogleCalendarConnection.Status.DISCONNECTED,
                    "oauth_state": "expired-attempt-state",
                    "oauth_attempt_expires_at": timezone.now() - timedelta(minutes=1),
                    "reconciliation_completed_at": None,
                },
                "lifecycle_recovery_complete",
            ),
        ],
        ids=[
            "credential-mismatch",
            "overdue",
            "verification-incomplete",
            "lifecycle-incomplete",
            "connected-error",
            "expired-oauth-attempt",
        ],
    )
    def test_incomplete_readiness_contracts_fail_closed(
        self,
        session_client,
        calendar_instance_admin,
        connection_values,
        failed_contract,
    ):
        _verified_connection(**connection_values)

        with patch(
            "plane.app.views.integration.get_google_calendar_oauth_credentials",
            return_value=OAUTH_CREDENTIALS,
        ):
            response = session_client.get(reverse("google-calendar-release-readiness"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["ready"] is False
        if failed_contract == "reconciliation_overdue":
            assert response.data[failed_contract] is True
        else:
            assert response.data[failed_contract] is False

    def test_non_admin_cannot_read_instance_readiness(self, api_client):
        response = api_client.get(reverse("google-calendar-release-readiness"))

        assert response.status_code in {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN}

    def test_entity_noop_production_log_does_not_claim_provider_success(self, caplog):
        connection = _verified_connection()
        missing_issue_id = uuid.uuid4()

        with (
            caplog.at_level(logging.INFO, logger="plane.worker"),
            patch("plane.bgtasks.google_calendar_task._client_for") as client_for,
        ):
            result = _synchronize_issue_for_connection(missing_issue_id, connection.id)

        assert result == "missing"
        client_for.assert_not_called()
        record = next(
            record for record in caplog.records if getattr(record, "operation", None) == "work_item_publication"
        )
        assert record.workspace_id == str(connection.workspace_integration.workspace_id)
        assert record.connection_id == str(connection.id)
        assert record.entity_id == str(missing_issue_id)
        assert record.outcome == "missing"
        assert record.attempt == 1
        assert record.google_status_class == "not_requested"
        assert record.calendar_generation == connection.calendar_generation
        assert record.reconciliation_action == "converge_entity"

    def test_local_only_lifecycle_production_log_does_not_claim_provider_success(self, caplog):
        connection = GoogleCalendarConnectionFactory(
            pending_cleanup=True,
            calendar_id="",
            calendar_operation_id=None,
            retain_grant_after_cleanup=True,
        )
        client = Mock()

        with (
            caplog.at_level(logging.INFO, logger="plane.worker"),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.bgtasks.google_calendar_task._client_for", return_value=client),
        ):
            result = reconcile_google_calendar_connection(str(connection.id), connection.lifecycle_generation)

        assert result == "disabled"
        client.delete_calendar.assert_not_called()
        client.revoke_grant.assert_not_called()
        record = next(
            record for record in caplog.records if getattr(record, "operation", None) == "lifecycle_reconciliation"
        )
        assert record.outcome == "disabled"
        assert record.google_status_class == "unknown"
        assert record.reconciliation_action == "cleanup"

    @pytest.mark.parametrize("outcome", ["missing", "stale"])
    def test_lifecycle_early_outcomes_have_production_telemetry(self, caplog, outcome):
        if outcome == "stale":
            connection = GoogleCalendarConnectionFactory(pending=True, lifecycle_generation=3)
            connection_id = str(connection.id)
            generation = 2
        else:
            connection_id = str(uuid.uuid4())
            generation = 1

        with (
            caplog.at_level(logging.INFO, logger="plane.worker"),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
        ):
            result = reconcile_google_calendar_connection(connection_id, generation)

        assert result == outcome
        record = next(
            record for record in caplog.records if getattr(record, "operation", None) == "lifecycle_reconciliation"
        )
        assert record.connection_id == connection_id
        assert record.outcome == outcome
        assert record.attempt == 1
        assert record.google_status_class == "not_requested"
        assert record.reconciliation_action == "prepare"

    def test_production_publication_failure_has_context_analytics_and_no_task_secrets(self, caplog):
        connection = _verified_connection()
        task = Mock()
        task.task = GOOGLE_CALENDAR_LIFECYCLE_TASK
        task.delay.side_effect = RuntimeError("forbidden-broker-message")

        with (
            caplog.at_level(logging.ERROR, logger="plane.integrations.google_calendar.dispatch"),
            patch("plane.integrations.google_calendar.dispatch.publish_google_calendar_analytics") as analytics,
            pytest.raises(RuntimeError, match="forbidden-broker-message"),
        ):
            publish_google_calendar_task(task, str(connection.id), "forbidden-secret-task-argument")

        record = next(record for record in caplog.records if getattr(record, "operation", None) == "task_publication")
        assert record.workspace_id == str(connection.workspace_integration.workspace_id)
        assert record.connection_id == str(connection.id)
        assert record.outcome == "failed"
        assert record.attempt == 1
        assert record.enqueue_latency_ms >= 0
        assert record.google_status_class == "not_requested"
        assert record.calendar_generation == connection.calendar_generation
        assert record.reconciliation_action == "reconcile_google_calendar_connection"
        assert record.publication_failure_class == "RuntimeError"
        analytics.assert_called_once()
        assert analytics.call_args.args == ("google_calendar_publication_failure",)
        assert analytics.call_args.kwargs["workspace_id"] == connection.workspace_integration.workspace_id
        assert analytics.call_args.kwargs["connection_id"] == connection.id
        serialized_record = repr(record.__dict__)
        serialized_analytics = repr(analytics.call_args)
        assert "forbidden-secret-task-argument" not in serialized_record
        assert "forbidden-secret-task-argument" not in serialized_analytics
        assert "forbidden-broker-message" not in serialized_record
        assert "forbidden-broker-message" not in serialized_analytics


@pytest.mark.contract
def test_structured_calendar_logs_are_complete_and_secret_free(caplog):
    logger = logging.getLogger("plane.tests.google_calendar.telemetry")
    workspace_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_google_calendar_operation(
            logger,
            "provider_inventory",
            workspace_id=workspace_id,
            connection_id=connection_id,
            entity_id=entity_id,
            outcome="continued",
            attempt=2,
            enqueue_latency_ms=12.5,
            google_status_class="success",
            inventory_mode="incremental",
            provider_page_count=5,
            local_candidate_count=37,
            calendar_generation=4,
            reconciliation_action="continue_local_scan",
            last_completion_age_seconds=7210,
            publication_failure_class="BrokerError",
            access_token="forbidden-access-token",
            oauth_state="forbidden-state-digest",
            credential_fingerprint="forbidden-fingerprint",
            provider_email="forbidden-provider@example.com",
            event_description="forbidden-event-content",
            task_args=("forbidden-task-secret",),
        )

    record = caplog.records[-1]
    assert record.operation == "provider_inventory"
    for field, expected in {
        "workspace_id": str(workspace_id),
        "connection_id": str(connection_id),
        "entity_id": str(entity_id),
        "outcome": "continued",
        "attempt": 2,
        "enqueue_latency_ms": 12.5,
        "google_status_class": "success",
        "inventory_mode": "incremental",
        "provider_page_count": 5,
        "local_candidate_count": 37,
        "calendar_generation": 4,
        "reconciliation_action": "continue_local_scan",
        "last_completion_age_seconds": 7210,
        "publication_failure_class": "BrokerError",
    }.items():
        assert getattr(record, field) == expected
    serialized_record = repr(record.__dict__)
    for forbidden in (
        "forbidden-access-token",
        "forbidden-state-digest",
        "forbidden-fingerprint",
        "forbidden-provider@example.com",
        "forbidden-event-content",
        "forbidden-task-secret",
    ):
        assert forbidden not in serialized_record


@pytest.mark.contract
def test_calendar_analytics_are_allowlisted_and_secret_free():
    workspace_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    with patch("plane.bgtasks.event_tracking_task.track_event.delay") as track:
        assert publish_google_calendar_analytics(
            "google_calendar_inventory_reset",
            user_id="member-id",
            workspace_id=workspace_id,
            workspace_slug="workspace-slug",
            connection_id=connection_id,
            outcome="reset",
            attempt=2,
            google_status_class="4xx",
            inventory_mode="full",
            provider_page_count=1,
            calendar_generation=3,
            reconciliation_action="reset_inventory",
            access_token="forbidden-access-token",
            oauth_state="forbidden-state-digest",
            credential_fingerprint="forbidden-fingerprint",
            provider_account_id="forbidden-provider-account",
            event_description="forbidden-event-content",
            task_args=("forbidden-task-secret",),
        )

    properties = track.call_args.kwargs["event_properties"]
    assert properties == {
        "workspace_id": str(workspace_id),
        "connection_id": str(connection_id),
        "outcome": "reset",
        "attempt": 2,
        "google_status_class": "4xx",
        "inventory_mode": "full",
        "provider_page_count": 1,
        "calendar_generation": 3,
        "reconciliation_action": "reset_inventory",
    }
    serialized_call = repr(track.call_args)
    for forbidden in (
        "forbidden-access-token",
        "forbidden-state-digest",
        "forbidden-fingerprint",
        "forbidden-provider-account",
        "forbidden-event-content",
        "forbidden-task-secret",
    ):
        assert forbidden not in serialized_call
