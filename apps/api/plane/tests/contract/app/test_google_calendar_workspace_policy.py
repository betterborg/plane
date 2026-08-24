# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from django.test import override_settings
from django.urls import reverse
from rest_framework import status

from plane.bgtasks.google_calendar_task import (
    backfill_google_calendar_cycles,
    backfill_google_calendar_open_issues,
    reconcile_google_calendar_connection,
    reconcile_google_calendar_workspace_issue_resyncs,
    resync_google_calendar_workspace_cycles,
    resync_google_calendar_workspace_issues,
    synchronize_google_calendar_issue,
    synchronize_google_calendar_cycle,
)
from plane.db.models import (
    GoogleCalendarConnection,
    GoogleCalendarEvent,
    Label,
    WorkspaceIntegration,
    WorkspaceMember,
)
from plane.db.signals import suppress_google_calendar_issue_signal_dispatch
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_CYCLE_BACKFILL_TASK,
    GOOGLE_CALENDAR_ISSUE_SYNC_TASK,
    GOOGLE_CALENDAR_LIFECYCLE_TASK,
    GOOGLE_CALENDAR_OPEN_BACKFILL_TASK,
    GOOGLE_CALENDAR_CYCLE_SYNC_TASK,
    GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_METADATA_KEY,
    GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_TASK,
    GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY,
    GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK,
)
from plane.tests.factories import (
    CycleFactory,
    CycleIssueFactory,
    GoogleCalendarConnectionFactory,
    IntegrationFactory,
    IssueAssigneeFactory,
    IssueFactory,
    ProjectFactory,
    StateFactory,
    UserFactory,
    WorkspaceFactory,
    WorkspaceIntegrationFactory,
    WorkspaceMemberFactory,
)


@pytest.fixture
def calendar_integration(db):
    return IntegrationFactory(title="Google Calendar", provider="google_calendar")


def _policy_url(workspace):
    return reverse("google-calendar-workspace-policy", kwargs={"slug": workspace.slug})


def _provider_client():
    client = Mock()
    client.access_token = None
    client.get_event.return_value = {"id": "existing-event"}
    client.list_events.return_value = []
    return client


@pytest.mark.contract
class TestGoogleCalendarWorkspacePolicy:
    @pytest.mark.django_db
    def test_unreleased_policy_is_not_found(self, session_client, workspace, calendar_integration):
        with override_settings(GOOGLE_CALENDAR_RELEASED=False):
            response = session_client.patch(_policy_url(workspace), {"enabled": False}, format="json")

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert not WorkspaceIntegration.objects.filter(
            workspace=workspace,
            integration=calendar_integration,
        ).exists()

    @pytest.mark.django_db
    def test_only_role_20_can_patch_policy(self, session_client, workspace, create_user, calendar_integration):
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=15)

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.patch(_policy_url(workspace), {"enabled": False}, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not WorkspaceIntegration.objects.filter(
            workspace=workspace,
            integration=calendar_integration,
        ).exists()

    @pytest.mark.django_db
    def test_enable_rejects_incomplete_instance_credentials(
        self,
        session_client,
        workspace,
        calendar_integration,
    ):
        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.patch(_policy_url(workspace), {"enabled": True}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data == {"error": "google_calendar_credentials_incomplete"}
        assert not WorkspaceIntegration.objects.filter(
            workspace=workspace,
            integration=calendar_integration,
        ).exists()

    @pytest.mark.django_db
    def test_filter_policy_requires_label_or_priority(
        self,
        session_client,
        workspace,
        calendar_integration,
    ):
        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.patch(
                _policy_url(workspace),
                {
                    "enabled": False,
                    "mode": "filter",
                    "label_ids": [],
                    "priorities": [],
                },
                format="json",
            )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "non_field_errors" in response.data

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        ("field", "value"),
        (
            ("priorities", ["critical"]),
            ("label_match", "none"),
            ("recipients", "individual_members"),
        ),
    )
    def test_policy_rejects_unsupported_choice(
        self,
        session_client,
        workspace,
        calendar_integration,
        field,
        value,
    ):
        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.patch(
                _policy_url(workspace),
                {"enabled": False, field: value},
                format="json",
            )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert field in response.data
        assert not WorkspaceIntegration.objects.filter(
            workspace=workspace,
            integration=calendar_integration,
        ).exists()

    @pytest.mark.django_db
    @pytest.mark.parametrize("label_workspace", ("missing", "foreign", "mixed"))
    def test_policy_rejects_label_outside_workspace(
        self,
        session_client,
        workspace,
        calendar_integration,
        label_workspace,
    ):
        label_ids = [uuid4()]
        if label_workspace in {"foreign", "mixed"}:
            foreign_workspace = WorkspaceFactory()
            foreign_label_id = Label.objects.create(name="Foreign Calendar Label", workspace=foreign_workspace).id
            label_ids = [foreign_label_id]
        if label_workspace == "mixed":
            workspace_label_id = Label.objects.create(name="Workspace Calendar Label", workspace=workspace).id
            label_ids.insert(0, workspace_label_id)

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.patch(
                _policy_url(workspace),
                {"enabled": False, "mode": "filter", "label_ids": label_ids},
                format="json",
            )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data == {"label_ids": ["One or more labels do not belong to this workspace"]}
        assert not WorkspaceIntegration.objects.filter(
            workspace=workspace,
            integration=calendar_integration,
        ).exists()

    @pytest.mark.django_db
    def test_policy_accepts_workspace_label_and_canonical_priority(
        self,
        session_client,
        workspace,
        calendar_integration,
    ):
        label = Label.objects.create(name="Calendar Label", workspace=workspace)

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.patch(
                _policy_url(workspace),
                {
                    "enabled": False,
                    "mode": "filter",
                    "label_ids": [label.id],
                    "priorities": ["urgent", "high"],
                    "label_match": "all",
                },
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["label_ids"] == [str(label.id)]
        assert response.data["priorities"] == ["urgent", "high"]
        assert response.data["label_match"] == "all"

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.parametrize(
        ("previous_recipients", "current_recipients"),
        (
            ("workspace_members", "cycle_members"),
            ("cycle_members", "project_members"),
            ("project_members", "workspace_members"),
        ),
    )
    def test_each_recipient_mode_publishes_cycle_resync_after_policy_commit(
        self,
        session_client,
        workspace,
        calendar_integration,
        previous_recipients,
        current_recipients,
    ):
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration=calendar_integration,
            config={"enabled": True, "recipients": previous_recipients},
        )
        cycle_resync_task = Mock()
        committed_recipients = []

        def record_policy_after_commit(workspace_id, *, policy_generation):
            workspace_integration.refresh_from_db()
            committed_recipients.append((workspace_id, policy_generation, workspace_integration.config["recipients"]))

        cycle_resync_task.delay.side_effect = record_policy_after_commit
        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.app.views.integration._has_complete_google_calendar_credentials", return_value=True),
            patch("plane.app.views.integration.request_google_calendar_workspace_reconciliation", return_value=[]),
            patch(
                "plane.integrations.google_calendar.dispatch.current_app.signature",
                return_value=cycle_resync_task,
            ) as signature,
        ):
            response = session_client.patch(
                _policy_url(workspace),
                {"enabled": True, "recipients": current_recipients},
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["recipients"] == current_recipients
        signature.assert_called_once_with(GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_TASK)
        cycle_resync_task.delay.assert_called_once()
        assert committed_recipients[0][0] == str(workspace.id)
        assert committed_recipients[0][1]
        assert committed_recipients[0][2] == current_recipients

    @pytest.mark.django_db(transaction=True)
    def test_completion_and_filter_changes_share_one_after_commit_resync_path(
        self,
        session_client,
        workspace,
        calendar_integration,
    ):
        base_policy = {
            "enabled": True,
            "mode": "assignment",
            "update_on_completion": True,
            "recipients": "cycle_members",
            "label_ids": [],
            "priorities": [],
            "label_match": "any",
        }
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration=calendar_integration,
            config=base_policy,
        )
        label = Label.objects.create(name="Calendar Label", workspace=workspace)
        policies = (
            {**base_policy, "update_on_completion": False},
            {
                **base_policy,
                "mode": "filter",
                "update_on_completion": False,
                "priorities": ["urgent"],
            },
            {
                **base_policy,
                "mode": "filter",
                "update_on_completion": False,
                "label_ids": [str(label.id)],
                "priorities": ["high"],
            },
            {
                **base_policy,
                "mode": "filter",
                "update_on_completion": False,
                "label_ids": [str(label.id)],
                "priorities": ["high"],
                "label_match": "all",
            },
            {
                **base_policy,
                "mode": "filter",
                "update_on_completion": False,
                "label_ids": [str(label.id)],
                "priorities": ["high"],
                "label_match": "all",
                "recipients": "project_members",
            },
        )
        workspace_resync_task = Mock()
        committed_policies = []

        def record_policy_after_commit(workspace_id, *, policy_generation):
            workspace_integration.refresh_from_db()
            committed_policies.append((workspace_id, policy_generation, workspace_integration.config))

        workspace_resync_task.delay.side_effect = record_policy_after_commit
        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.app.views.integration._has_complete_google_calendar_credentials", return_value=True),
            patch("plane.app.views.integration.request_google_calendar_workspace_reconciliation", return_value=[]),
            patch(
                "plane.integrations.google_calendar.dispatch.current_app.signature",
                return_value=workspace_resync_task,
            ),
        ):
            for call_count, policy in enumerate(policies, start=1):
                response = session_client.patch(_policy_url(workspace), policy, format="json")

                assert response.status_code == status.HTTP_200_OK
                assert response.data == policy
                assert workspace_resync_task.delay.call_count == call_count
                assert committed_policies[-1][2] == policy

        assert {published[0] for published in committed_policies} == {str(workspace.id)}
        assert len({published[1] for published in committed_policies}) == len(policies)

    @pytest.mark.django_db
    def test_reenable_conflict_preserves_policy_and_generation(
        self,
        session_client,
        workspace,
        calendar_integration,
    ):
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration=calendar_integration,
            config={
                "enabled": False,
                "mode": "assignment",
                "update_on_completion": True,
                "recipients": "cycle_members",
                "label_ids": [],
                "priorities": [],
                "label_match": "any",
            },
        )
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="google-account",
            calendar_id="calendar-awaiting-delete",
            refresh_token="validated-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=4,
        )

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.app.views.integration._has_complete_google_calendar_credentials", return_value=True),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
        ):
            response = session_client.patch(_policy_url(workspace), {"enabled": True}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data == {"error": "google_calendar_disable_cleanup_in_progress"}
        workspace_integration.refresh_from_db()
        connection.refresh_from_db()
        assert workspace_integration.config["enabled"] is False
        assert connection.lifecycle_generation == 4
        assert connection.calendar_id == "calendar-awaiting-delete"

    @pytest.mark.django_db(transaction=True)
    def test_broker_failure_keeps_committed_policy_and_exact_generation(
        self,
        session_client,
        workspace,
        calendar_integration,
    ):
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration=calendar_integration,
            config={"enabled": False},
        )
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            provider_account_id="google-account",
            refresh_token="validated-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.DISCONNECTED,
            lifecycle_generation=4,
        )
        lifecycle_task = Mock()
        lifecycle_task.delay.side_effect = RuntimeError("broker unavailable")

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.app.views.integration._has_complete_google_calendar_credentials", return_value=True),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.app.views.integration.current_app.signature", return_value=lifecycle_task),
        ):
            response = session_client.patch(_policy_url(workspace), {"enabled": True}, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {
            "enabled": True,
            "mode": "assignment",
            "update_on_completion": True,
            "recipients": "cycle_members",
            "label_ids": [],
            "priorities": [],
            "label_match": "any",
        }
        workspace_integration.refresh_from_db()
        connection.refresh_from_db()
        assert workspace_integration.config == response.data
        assert connection.desired_state == GoogleCalendarConnection.DesiredState.CONNECTED
        assert connection.status == GoogleCalendarConnection.Status.PENDING
        assert connection.lifecycle_generation == 5
        lifecycle_task.delay.assert_called_once_with(str(connection.id), 5)

    @pytest.mark.django_db(transaction=True)
    def test_completion_policy_update_resyncs_terminal_and_reopened_events_after_commit(
        self,
        session_client,
        workspace,
        create_user,
        calendar_integration,
    ):
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration=calendar_integration,
            config={"enabled": True, "mode": "assignment", "update_on_completion": True},
        )
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            member=create_user,
            active=True,
        )
        project = ProjectFactory(workspace=workspace)
        completed_state = StateFactory(project=project, group="completed", name="Done")
        cancelled_state = StateFactory(project=project, group="cancelled", name="Cancelled")
        reopened_state = StateFactory(project=project, group="started", name="In progress")
        with suppress_google_calendar_issue_signal_dispatch():
            completed = IssueFactory(project=project, state=completed_state)
            cancelled = IssueFactory(project=project, state=cancelled_state)
            reopened = IssueFactory(project=project, state=reopened_state)
            for issue in (completed, cancelled, reopened):
                IssueAssigneeFactory(issue=issue, assignee=create_user, project=project)
        for index, issue in enumerate((completed, cancelled, reopened)):
            GoogleCalendarEvent.objects.create(
                connection=connection,
                entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
                entity_id=issue.id,
                google_event_id=f"existing-event-{index}",
                payload_hash=f"stale-payload-{index}",
            )

        provider_client = _provider_client()
        workspace_resync_task = Mock()

        def publish_issue_immediately(task, issue_id):
            synchronize_google_calendar_issue.run(issue_id)

        def resync_after_commit(workspace_id, *, policy_generation):
            workspace_integration.refresh_from_db()
            assert workspace_integration.config["update_on_completion"] is False
            return resync_google_calendar_workspace_issues.run(
                workspace_id,
                policy_generation=policy_generation,
            )

        workspace_resync_task.delay.side_effect = resync_after_commit
        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.app.views.integration._has_complete_google_calendar_credentials", return_value=True),
            patch("plane.app.views.integration.request_google_calendar_workspace_reconciliation", return_value=[]),
            patch(
                "plane.integrations.google_calendar.dispatch.current_app.signature",
                return_value=workspace_resync_task,
            ),
            patch(
                "plane.bgtasks.google_calendar_task.publish_google_calendar_task",
                side_effect=publish_issue_immediately,
            ),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=provider_client),
        ):
            response = session_client.patch(
                _policy_url(workspace),
                {"enabled": True, "update_on_completion": False},
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK
        workspace_integration.refresh_from_db()
        generation = workspace_resync_task.delay.call_args.kwargs["policy_generation"]
        workspace_resync_task.delay.assert_called_once_with(
            str(workspace.id),
            policy_generation=generation,
        )
        assert GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY not in workspace_integration.metadata
        assert set(GoogleCalendarEvent.objects.filter(connection=connection).values_list("entity_id", flat=True)) == {
            reopened.id
        }
        assert provider_client.delete_event.call_count == 2
        reopened_payload = provider_client.update_event.call_args.args[2]
        assert reopened_payload["summary"].startswith(f"[{project.identifier}-")
        assert "colorId" not in reopened_payload
        assert reopened_payload["status"] == "confirmed"

    @pytest.mark.django_db(transaction=True)
    def test_completion_policy_delete_to_update_restores_terminal_events(
        self,
        session_client,
        workspace,
        create_user,
        calendar_integration,
    ):
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration=calendar_integration,
            config={"enabled": True, "mode": "assignment", "update_on_completion": False},
        )
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            member=create_user,
            active=True,
        )
        project = ProjectFactory(workspace=workspace)
        with suppress_google_calendar_issue_signal_dispatch():
            completed = IssueFactory(
                project=project,
                state=StateFactory(project=project, group="completed", name="Done"),
            )
            cancelled = IssueFactory(
                project=project,
                state=StateFactory(project=project, group="cancelled", name="Cancelled"),
            )
            for issue in (completed, cancelled):
                IssueAssigneeFactory(issue=issue, assignee=create_user, project=project)

        provider_client = _provider_client()
        workspace_resync_task = Mock()
        workspace_resync_task.delay.side_effect = resync_google_calendar_workspace_issues.run

        def publish_issue_immediately(task, issue_id):
            synchronize_google_calendar_issue.run(issue_id)

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.app.views.integration._has_complete_google_calendar_credentials", return_value=True),
            patch("plane.app.views.integration.request_google_calendar_workspace_reconciliation", return_value=[]),
            patch(
                "plane.integrations.google_calendar.dispatch.current_app.signature",
                return_value=workspace_resync_task,
            ),
            patch(
                "plane.bgtasks.google_calendar_task.publish_google_calendar_task",
                side_effect=publish_issue_immediately,
            ),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=provider_client),
        ):
            response = session_client.patch(
                _policy_url(workspace),
                {"enabled": True, "update_on_completion": True},
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK
        assert GoogleCalendarEvent.objects.filter(connection=connection).count() == 2
        inserted_summaries = {call.args[2]["summary"] for call in provider_client.insert_event.call_args_list}
        assert any(summary.startswith("[Completed]") for summary in inserted_summaries)
        assert any(summary.startswith("[Cancelled]") for summary in inserted_summaries)

    @pytest.mark.django_db(transaction=True)
    def test_completion_resync_publication_failure_is_rediscovered_by_reconciliation(
        self,
        session_client,
        workspace,
        create_user,
        calendar_integration,
    ):
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration=calendar_integration,
            config={"enabled": True, "mode": "assignment", "update_on_completion": True},
        )
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            member=create_user,
            active=True,
        )
        project = ProjectFactory(workspace=workspace)
        with suppress_google_calendar_issue_signal_dispatch():
            completed = IssueFactory(
                project=project,
                state=StateFactory(project=project, group="completed", name="Done"),
            )
            IssueAssigneeFactory(issue=completed, assignee=create_user, project=project)
        GoogleCalendarEvent.objects.create(
            connection=connection,
            entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
            entity_id=completed.id,
            google_event_id="completed-event",
            payload_hash="completed-payload",
        )
        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.app.views.integration._has_complete_google_calendar_credentials", return_value=True),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.integrations.google_calendar.dispatch.publish_google_calendar_task",
                side_effect=RuntimeError("broker unavailable"),
            ) as failed_publish,
        ):
            response = session_client.patch(
                _policy_url(workspace),
                {"enabled": True, "update_on_completion": False},
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK
        workspace_integration.refresh_from_db()
        connection.refresh_from_db()
        assert workspace_integration.config["update_on_completion"] is False
        assert connection.status == GoogleCalendarConnection.Status.PENDING
        assert connection.lifecycle_generation == 2
        assert GoogleCalendarEvent.objects.filter(connection=connection, entity_id=completed.id).exists()
        assert GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY in workspace_integration.metadata
        assert failed_publish.call_count == 2
        assert [publication.args[0].task for publication in failed_publish.call_args_list] == [
            GOOGLE_CALENDAR_LIFECYCLE_TASK,
            GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK,
        ]

        provider_client = _provider_client()

        def publish_immediately(task, *args, **kwargs):
            if task.task == GOOGLE_CALENDAR_LIFECYCLE_TASK:
                return reconcile_google_calendar_connection.run(*args, **kwargs)
            if task.task == GOOGLE_CALENDAR_OPEN_BACKFILL_TASK:
                return backfill_google_calendar_open_issues.run(*args, **kwargs)
            if task.task == GOOGLE_CALENDAR_CYCLE_BACKFILL_TASK:
                return backfill_google_calendar_cycles.run(*args, **kwargs)
            if task.task == GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK:
                return resync_google_calendar_workspace_issues.run(*args, **kwargs)
            if task.task == GOOGLE_CALENDAR_ISSUE_SYNC_TASK:
                return synchronize_google_calendar_issue.run(*args, **kwargs)
            raise AssertionError(f"Unexpected reconciliation task: {task.task}")

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.bgtasks.google_calendar_task.publish_google_calendar_task",
                side_effect=publish_immediately,
            ),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=provider_client),
        ):
            discovered = reconcile_google_calendar_workspace_issue_resyncs.run()

        assert discovered == 1
        connection.refresh_from_db()
        assert connection.status == GoogleCalendarConnection.Status.ACTIVE
        assert not GoogleCalendarEvent.objects.filter(connection=connection, entity_id=completed.id).exists()
        provider_client.delete_event.assert_called_once_with(connection.calendar_id, "completed-event")
        workspace_integration.refresh_from_db()
        assert GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY not in workspace_integration.metadata

    @pytest.mark.django_db(transaction=True)
    def test_recipient_resync_publication_failure_keeps_policy_and_recovers_cycle_drift(
        self,
        session_client,
        workspace,
        calendar_integration,
    ):
        workspace_integration = WorkspaceIntegrationFactory(
            workspace=workspace,
            integration=calendar_integration,
            config={"enabled": True, "recipients": "cycle_members"},
        )
        former_connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            member=UserFactory(),
            active=True,
        )
        new_member_connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            active=True,
        )
        WorkspaceMemberFactory(workspace=workspace, member=new_member_connection.member)
        cycle = CycleFactory(project__workspace=workspace)
        issue = IssueFactory(project=cycle.project)
        CycleIssueFactory(cycle=cycle, issue=issue, project=cycle.project)
        IssueAssigneeFactory(issue=issue, assignee=former_connection.member, project=cycle.project)
        GoogleCalendarEvent.objects.create(
            connection=former_connection,
            entity_type=GoogleCalendarEvent.EntityType.CYCLE,
            entity_id=cycle.id,
            google_event_id="former-cycle-event",
            payload_hash="former-cycle-payload",
        )

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.app.views.integration._has_complete_google_calendar_credentials", return_value=True),
            patch("plane.app.views.integration.request_google_calendar_workspace_reconciliation", return_value=[]),
            patch(
                "plane.integrations.google_calendar.dispatch.publish_google_calendar_task",
                side_effect=RuntimeError("broker unavailable"),
            ) as failed_publish,
        ):
            response = session_client.patch(
                _policy_url(workspace),
                {"enabled": True, "recipients": "workspace_members"},
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK
        workspace_integration.refresh_from_db()
        assert workspace_integration.config["recipients"] == "workspace_members"
        assert GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_METADATA_KEY in workspace_integration.metadata
        failed_publish.assert_called_once()

        provider_client = _provider_client()

        def publish_immediately(task, *args, **kwargs):
            if task.task == GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_TASK:
                return resync_google_calendar_workspace_cycles.run(*args, **kwargs)
            if task.task == GOOGLE_CALENDAR_CYCLE_SYNC_TASK:
                return synchronize_google_calendar_cycle.run(*args, **kwargs)
            raise AssertionError(f"Unexpected reconciliation task: {task.task}")

        with (
            patch(
                "plane.bgtasks.google_calendar_task.publish_google_calendar_task",
                side_effect=publish_immediately,
            ),
            patch("plane.bgtasks.google_calendar_task.GoogleCalendarClient", return_value=provider_client),
        ):
            discovered = reconcile_google_calendar_workspace_issue_resyncs.run()

        assert discovered == 1
        assert set(
            GoogleCalendarEvent.objects.filter(
                entity_type=GoogleCalendarEvent.EntityType.CYCLE,
                entity_id=cycle.id,
            ).values_list("connection_id", flat=True)
        ) == {new_member_connection.id}
        workspace_integration.refresh_from_db()
        assert GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_METADATA_KEY not in workspace_integration.metadata
