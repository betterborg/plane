# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from unittest.mock import Mock, call, patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.db import transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from plane.app.serializers.issue import IssueCreateSerializer
from plane.app.views.google_calendar_oauth import GOOGLE_CALENDAR_OAUTH_SESSION_KEY
from plane.bgtasks.google_calendar_task import (
    _mark_authorization_failure,
    _send_google_calendar_disconnected_email,
    reconcile_google_calendar_workspace_issue_resyncs,
    schedule_google_calendar_reconciliations,
    send_google_calendar_disconnected_email,
)
from plane.db.models import (
    Cycle,
    CycleIssue,
    GoogleCalendarConnection,
    Issue,
    IssueAssignee,
    IssueLabel,
    Label,
    Notification,
    Profile,
    Project,
    State,
    Workspace,
    WorkspaceMember,
)
from plane.integrations.google_calendar.client import GoogleCalendarInvalidGrant
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_CYCLE_SYNC_TASK,
    GOOGLE_CALENDAR_INVENTORY_TASK,
    GOOGLE_CALENDAR_ISSUE_SYNC_TASK,
    GOOGLE_CALENDAR_LABEL_ISSUE_RESYNC_TASK,
    GOOGLE_CALENDAR_LIFECYCLE_TASK,
    GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK,
    GOOGLE_CALENDAR_STATE_ISSUE_RESYNC_TASK,
    GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY,
    GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK,
)
from plane.integrations.google_calendar.oauth import (
    GOOGLE_CALENDAR_LIST_SCOPE,
    GOOGLE_CALENDAR_SCOPE,
    GoogleCalendarOAuthCredentials,
    GoogleCalendarOAuthGrant,
    GoogleCalendarOAuthIdentity,
)
from plane.tests.factories import (
    CycleFactory,
    GoogleCalendarConnectionFactory,
    IntegrationFactory,
    IssueFactory,
    LabelFactory,
    ProjectFactory,
    ProjectMemberFactory,
    StateFactory,
    UserFactory,
    WorkspaceFactory,
    WorkspaceIntegrationFactory,
    WorkspaceMemberFactory,
)


def _task_name(task):
    return getattr(task, "task", None) or getattr(task, "name", None)


def _calendar_integration(workspace, *, enabled=True):
    return WorkspaceIntegrationFactory(
        workspace=workspace,
        integration=IntegrationFactory(title="Google Calendar", provider="google_calendar"),
        config={"enabled": enabled},
    )


def _active_connection(workspace_integration, member, *, generation=3):
    return GoogleCalendarConnectionFactory(
        workspace_integration=workspace_integration,
        member=member,
        active=True,
        calendar_id=f"calendar-{member.id}",
        lifecycle_generation=generation,
        reconciliation_completed_at=timezone.now() - timedelta(days=2),
    )


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarBrokerFailureReleaseGate:
    def test_content_mutation_owners_commit_and_periodic_reconciliation_rediscovers_drift(
        self,
        session_client,
        api_key_client,
        workspace,
        create_user,
    ):
        """One failed broker must not turn any content owner into a failed write."""

        workspace_integration = _calendar_integration(workspace)
        connection = _active_connection(workspace_integration, create_user)
        project = ProjectFactory(workspace=workspace)
        ProjectMemberFactory(project=project, member=create_user, role=20)
        state = StateFactory(project=project)
        label = LabelFactory(project=project)
        observed_publications = []

        def fail_after_observing_committed_state(task, *args, **kwargs):
            task_name = _task_name(task)
            observed_publications.append((task_name, args, kwargs))
            if task_name == GOOGLE_CALENDAR_ISSUE_SYNC_TASK:
                assert Issue.all_objects.filter(id=args[0]).exists()
            elif task_name == GOOGLE_CALENDAR_STATE_ISSUE_RESYNC_TASK:
                saved_state = State.objects.get(id=args[0])
                assert saved_state.name == "Ready for release"
            elif task_name == GOOGLE_CALENDAR_LABEL_ISSUE_RESYNC_TASK:
                saved_label = Label.objects.get(id=args[0])
                assert saved_label.name == "Release label"
            elif task_name == GOOGLE_CALENDAR_CYCLE_SYNC_TASK:
                saved_cycle = Cycle.all_objects.get(id=args[0])
                assert saved_cycle.id
            elif task_name == GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK:
                saved_project = Project.objects.get(id=args[0])
                assert saved_project.google_calendar_sync_enabled is False
            raise RuntimeError("broker unavailable")

        app_payload = {
            "name": "App mutation",
            "state_id": str(state.id),
            "assignee_ids": [str(create_user.id)],
            "label_ids": [str(label.id)],
        }
        public_payload = {
            "name": "Public mutation",
            "state": str(state.id),
            "assignees": [str(create_user.id)],
            "labels": [str(label.id)],
        }

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch(
                "plane.integrations.google_calendar.dispatch.publish_google_calendar_task",
                side_effect=fail_after_observing_committed_state,
            ),
            patch("plane.app.views.issue.base.issue_activity.delay"),
            patch("plane.db.mixins.soft_delete_related_objects.delay"),
        ):
            serializer = IssueCreateSerializer(
                data=app_payload,
                context={
                    "project_id": project.id,
                    "workspace_id": workspace.id,
                    "default_assignee_id": None,
                },
            )
            assert serializer.is_valid(), serializer.errors
            app_issue = serializer.save()

            public_response = api_key_client.post(
                f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/work-items/",
                public_payload,
                format="json",
            )
            assert public_response.status_code == status.HTTP_201_CREATED, public_response.data

            bulk_issue = IssueFactory(project=project, state=state)
            bulk_response = session_client.post(
                reverse(
                    "project-issue-dates",
                    kwargs={"slug": workspace.slug, "project_id": project.id},
                ),
                {
                    "updates": [
                        {
                            "id": str(bulk_issue.id),
                            "start_date": "2026-09-01",
                            "target_date": "2026-09-02",
                        }
                    ]
                },
                format="json",
            )
            assert bulk_response.status_code == status.HTTP_200_OK, bulk_response.data

            state_response = session_client.patch(
                f"/api/workspaces/{workspace.slug}/projects/{project.id}/states/{state.id}/",
                {"name": "Ready for release"},
                format="json",
            )
            assert state_response.status_code == status.HTTP_200_OK, state_response.data

            label_response = api_key_client.patch(
                f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/labels/{label.id}/",
                {"name": "Release label"},
                format="json",
            )
            assert label_response.status_code == status.HTTP_200_OK, label_response.data

            cycle = CycleFactory(project=project)
            cycle.name = "Release cycle"
            cycle.save(update_fields=["name", "updated_at"])

            membership_issue = IssueFactory(project=project, state=state)
            membership_cycle = CycleFactory(project=project)
            membership_response = session_client.post(
                f"/api/workspaces/{workspace.slug}/projects/{project.id}/cycles/{membership_cycle.id}/cycle-issues/",
                {"issues": [str(membership_issue.id)]},
                format="json",
            )
            assert membership_response.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED)

            project_response = session_client.patch(
                reverse(
                    "google-calendar-project-sync",
                    kwargs={"slug": workspace.slug, "project_id": project.id},
                ),
                {"google_calendar_sync_enabled": False},
                format="json",
            )
            assert project_response.status_code == status.HTTP_200_OK, project_response.data

        app_issue.refresh_from_db()
        bulk_issue.refresh_from_db()
        state.refresh_from_db()
        label.refresh_from_db()
        cycle.refresh_from_db()
        project.refresh_from_db()
        assert app_issue.name == "App mutation"
        assert set(IssueAssignee.objects.filter(issue=app_issue).values_list("assignee_id", flat=True)) == {
            create_user.id
        }
        assert set(IssueLabel.objects.filter(issue=app_issue).values_list("label_id", flat=True)) == {label.id}
        assert bulk_issue.start_date.isoformat() == "2026-09-01"
        assert bulk_issue.target_date.isoformat() == "2026-09-02"
        assert state.name == "Ready for release"
        assert label.name == "Release label"
        assert cycle.name == "Release cycle"
        assert project.google_calendar_sync_enabled is False
        assert CycleIssue.objects.filter(cycle=membership_cycle, issue=membership_issue).exists()

        published_task_names = {task_name for task_name, _args, _kwargs in observed_publications}
        assert {
            GOOGLE_CALENDAR_ISSUE_SYNC_TASK,
            GOOGLE_CALENDAR_STATE_ISSUE_RESYNC_TASK,
            GOOGLE_CALENDAR_LABEL_ISSUE_RESYNC_TASK,
            GOOGLE_CALENDAR_CYCLE_SYNC_TASK,
            GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK,
        } <= published_task_names

        recovered = Mock()
        recovered.set.return_value = recovered
        with patch(
            "plane.bgtasks.google_calendar_task.current_app.signature",
            return_value=recovered,
        ) as signature:
            discovered = schedule_google_calendar_reconciliations.run()

        assert discovered == 1
        signature.assert_called_once_with(GOOGLE_CALENDAR_INVENTORY_TASK)
        recovered.set.assert_called_once()
        recovered.delay.assert_called_once_with(str(connection.id))

    def test_oauth_policy_and_disconnect_keep_exact_durable_recovery_selectors(
        self,
        session_client,
        workspace,
        create_user,
    ):
        """Consent and policy writes retain exact generations and resync markers."""

        workspace_integration = _calendar_integration(workspace)
        credentials = GoogleCalendarOAuthCredentials("calendar-client", "calendar-secret")
        grant = GoogleCalendarOAuthGrant(
            access_token="access-token",
            refresh_token="refresh-token",
            token_expires_at=timezone.now() + timedelta(hours=1),
            scopes=frozenset({"openid", "email", GOOGLE_CALENDAR_SCOPE, GOOGLE_CALENDAR_LIST_SCOPE}),
        )
        identity = GoogleCalendarOAuthIdentity("google-account", create_user.email)
        failed_tasks = []
        later_callback = Mock()

        def fail_publication(task, *args, **kwargs):
            failed_tasks.append((_task_name(task), args, kwargs))
            raise RuntimeError("broker unavailable")

        with (
            override_settings(
                GOOGLE_CALENDAR_RELEASED=True,
                APP_BASE_URL="https://plane.example",
            ),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.app.views.google_calendar_oauth.get_google_calendar_oauth_credentials",
                return_value=credentials,
            ),
        ):
            start_response = session_client.get(reverse("google-calendar-oauth-start", kwargs={"slug": workspace.slug}))
        assert start_response.status_code == status.HTTP_302_FOUND
        callback_state = parse_qs(urlparse(start_response.url).query)["state"][0]
        assert session_client.session[GOOGLE_CALENDAR_OAUTH_SESSION_KEY]

        with (
            override_settings(
                GOOGLE_CALENDAR_RELEASED=True,
                APP_BASE_URL="https://plane.example",
            ),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.app.views.google_calendar_oauth.get_google_calendar_oauth_credentials",
                return_value=credentials,
            ),
            patch("plane.app.views.google_calendar_oauth.exchange_google_calendar_code", return_value=grant),
            patch("plane.app.views.google_calendar_oauth.get_google_calendar_identity", return_value=identity),
            patch(
                "plane.integrations.google_calendar.dispatch.publish_google_calendar_task",
                side_effect=fail_publication,
            ),
        ):
            callback_response = session_client.get(
                reverse("google-calendar-oauth-callback"),
                {"state": callback_state, "code": "callback-code"},
            )

        assert callback_response.status_code == status.HTTP_302_FOUND
        assert parse_qs(urlparse(callback_response.url).query) == {"google_calendar_oauth": ["success"]}
        connection = GoogleCalendarConnection.objects.get(workspace_integration=workspace_integration)
        assert connection.status == GoogleCalendarConnection.Status.PENDING
        assert connection.lifecycle_generation == 1
        assert connection.refresh_token == "refresh-token"

        selector_task = Mock()
        with patch(
            "plane.bgtasks.google_calendar_task.current_app.signature",
            return_value=selector_task,
        ):
            assert schedule_google_calendar_reconciliations.run() == 1
        selector_task.delay.assert_called_once_with(str(connection.id), 1)

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.app.views.integration._has_complete_google_calendar_credentials", return_value=True),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.integrations.google_calendar.dispatch.publish_google_calendar_task",
                side_effect=fail_publication,
            ),
            patch("plane.app.views.integration.publish_google_calendar_analytics", later_callback),
        ):
            policy_response = session_client.patch(
                reverse("google-calendar-workspace-policy", kwargs={"slug": workspace.slug}),
                {
                    "enabled": True,
                    "mode": "assignment",
                    "update_on_completion": False,
                    "recipients": "cycle_members",
                },
                format="json",
            )

        assert policy_response.status_code == status.HTTP_200_OK, policy_response.data
        workspace_integration.refresh_from_db()
        assert workspace_integration.config["update_on_completion"] is False
        connection.refresh_from_db()
        policy_generation = connection.lifecycle_generation
        assert policy_generation == 2
        issue_generation = workspace_integration.metadata[GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY]
        assert issue_generation
        later_callback.assert_not_called()

        recovered_policy_task = Mock()
        with patch(
            "plane.bgtasks.google_calendar_task.publish_google_calendar_task",
            side_effect=lambda task, *args, **kwargs: recovered_policy_task(task, *args, **kwargs),
        ):
            assert reconcile_google_calendar_workspace_issue_resyncs.run() == 1
        recovered_policy_task.assert_called_once()
        assert _task_name(recovered_policy_task.call_args.args[0]) == GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK
        assert recovered_policy_task.call_args.kwargs == {"policy_generation": issue_generation}

        connection.refresh_from_db()
        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.integrations.google_calendar.dispatch.publish_google_calendar_task",
                side_effect=fail_publication,
            ),
            transaction.atomic(),
        ):
            disconnect_response = session_client.delete(
                reverse(
                    "google-calendar-connection",
                    kwargs={"slug": workspace.slug, "member_id": create_user.id},
                )
            )

        assert disconnect_response.status_code == status.HTTP_202_ACCEPTED
        connection.refresh_from_db()
        assert connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert connection.lifecycle_generation == policy_generation + 1
        assert [task_name for task_name, _args, _kwargs in failed_tasks].count(GOOGLE_CALENDAR_LIFECYCLE_TASK) >= 2

        recovered_disconnect = Mock()
        with patch(
            "plane.bgtasks.google_calendar_task.current_app.signature",
            return_value=recovered_disconnect,
        ) as disconnect_signature:
            assert schedule_google_calendar_reconciliations.run() == 1
        disconnect_signature.assert_called_once_with(GOOGLE_CALENDAR_LIFECYCLE_TASK)
        recovered_disconnect.delay.assert_called_once_with(str(connection.id), connection.lifecycle_generation)

    def test_member_account_and_workspace_cleanup_survive_independent_callback_failures(
        self,
        session_client,
        create_user,
    ):
        """Identity deletion owners commit cleanup intent before any publication."""

        member_integration = WorkspaceIntegrationFactory(
            integration__provider="google_calendar",
            config={"enabled": True},
        )
        removed_member = UserFactory()
        member_row = WorkspaceMemberFactory(
            workspace=member_integration.workspace,
            member=removed_member,
            role=15,
        )
        member_connection = _active_connection(member_integration, removed_member, generation=4)
        failed_member_task = Mock()
        failed_member_task.delay.side_effect = RuntimeError("member broker unavailable")

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.integrations.google_calendar.signals.current_app.signature",
                return_value=failed_member_task,
            ),
        ):
            member_row.is_active = False
            member_row.save(update_fields=["is_active", "updated_at"])

        member_row.refresh_from_db()
        member_connection.refresh_from_db()
        assert member_row.is_active is False
        assert member_connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert member_connection.lifecycle_generation == 5

        Profile.objects.create(user=create_user)
        account_integration = WorkspaceIntegrationFactory(
            integration__provider="google_calendar",
            config={"enabled": True},
        )
        WorkspaceMemberFactory(workspace=account_integration.workspace, member=create_user, role=15)
        account_connection = _active_connection(account_integration, create_user, generation=7)
        failed_account_task = Mock()
        failed_account_task.delay.side_effect = RuntimeError("account broker unavailable")
        failed_email = Mock(side_effect=RuntimeError("email broker unavailable"))

        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch("plane.app.views.user.base.current_app.signature", return_value=failed_account_task),
            patch("plane.app.views.user.base.user_deactivation_email.delay", failed_email),
        ):
            account_response = session_client.delete("/api/users/me/")

        assert account_response.status_code == status.HTTP_204_NO_CONTENT
        create_user.refresh_from_db()
        account_connection.refresh_from_db()
        assert create_user.is_active is False
        assert account_connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert account_connection.lifecycle_generation == 8
        failed_email.assert_called_once()

        recovered_cleanup = Mock()
        with patch(
            "plane.bgtasks.google_calendar_task.current_app.signature",
            return_value=recovered_cleanup,
        ) as cleanup_signature:
            assert schedule_google_calendar_reconciliations.run() == 2
        assert cleanup_signature.call_args_list == [
            call(GOOGLE_CALENDAR_LIFECYCLE_TASK),
            call(GOOGLE_CALENDAR_LIFECYCLE_TASK),
        ]
        assert set(recovered_cleanup.delay.call_args_list) == {
            call(str(member_connection.id), 5),
            call(str(account_connection.id), 8),
        }

        workspace_owner = UserFactory()
        workspace_client = session_client
        workspace_client.force_authenticate(user=workspace_owner)
        doomed_workspace = WorkspaceFactory(
            name="Doomed workspace",
            slug=f"doomed-{workspace_owner.id}",
            owner=workspace_owner,
            created_by=workspace_owner,
        )
        WorkspaceMember.objects.create(workspace=doomed_workspace, member=workspace_owner, role=20)
        doomed_integration = _calendar_integration(doomed_workspace)
        doomed_connection = _active_connection(doomed_integration, workspace_owner, generation=9)
        Profile.objects.create(user=workspace_owner, last_workspace_id=doomed_workspace.id)
        failed_workspace_task = Mock()
        callback_order = []

        def fail_lifecycle(*args, **kwargs):
            callback_order.append("lifecycle")
            raise RuntimeError("workspace lifecycle broker unavailable")

        def fail_recursive_delete(*args, **kwargs):
            callback_order.append("recursive-delete")
            raise RuntimeError("recursive delete broker unavailable")

        def fail_analytics(*args, **kwargs):
            callback_order.append("analytics")
            raise RuntimeError("analytics broker unavailable")

        failed_workspace_task.delay.side_effect = fail_lifecycle
        with (
            patch("plane.integrations.google_calendar.lifecycle._acquire_advisory_xact_lock"),
            patch(
                "plane.app.views.workspace.base.current_app.signature",
                return_value=failed_workspace_task,
            ),
            patch(
                "plane.db.mixins.soft_delete_related_objects.delay",
                side_effect=fail_recursive_delete,
            ),
            patch("plane.app.views.workspace.base.track_event.delay", side_effect=fail_analytics),
        ):
            workspace_response = workspace_client.delete(f"/api/workspaces/{doomed_workspace.slug}/")

        assert workspace_response.status_code == status.HTTP_204_NO_CONTENT
        persisted_workspace = Workspace.all_objects.get(id=doomed_workspace.id)
        persisted_connection = GoogleCalendarConnection.all_objects.get(id=doomed_connection.id)
        assert persisted_workspace.deleted_at is not None
        assert persisted_connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING
        assert persisted_connection.lifecycle_generation == 10
        assert callback_order == ["lifecycle", "recursive-delete", "analytics"]

        recovered_workspace_cleanup = Mock()
        with patch(
            "plane.bgtasks.google_calendar_task.current_app.signature",
            return_value=recovered_workspace_cleanup,
        ):
            assert schedule_google_calendar_reconciliations.run() == 3
        assert call(str(persisted_connection.id), 10) in recovered_workspace_cleanup.delay.call_args_list

    def test_broken_notification_email_list_and_read_contract_share_reconnect_destination(
        self,
        session_client,
        workspace,
        create_user,
    ):
        """The null-project notice and both email bodies expose one member reconnect path."""

        connection = GoogleCalendarConnectionFactory(
            active=True,
            workspace_integration__workspace=workspace,
            member=create_user,
            provider_email=create_user.email,
        )
        reconnect_path = f"/{workspace.slug}/settings/integrations/google-calendar"
        reconnect_url = f"https://plane.example{reconnect_path}"

        failed_health_notice = Mock(side_effect=RuntimeError("health notice broker unavailable"))
        with patch.object(send_google_calendar_disconnected_email, "delay", failed_health_notice):
            _mark_authorization_failure(connection, GoogleCalendarInvalidGrant("refresh rejected"))
        failed_health_notice.assert_called_once()

        message = Mock()
        with (
            override_settings(APP_BASE_URL="https://plane.example"),
            patch(
                "plane.bgtasks.google_calendar_task.get_email_configuration",
                return_value=(
                    "smtp.example",
                    "smtp-user",
                    "smtp-password",
                    "587",
                    "1",
                    "0",
                    "plane@example.com",
                ),
            ),
            patch("plane.bgtasks.google_calendar_task.get_connection"),
            patch("plane.bgtasks.google_calendar_task.EmailMultiAlternatives", return_value=message) as email,
        ):
            _send_google_calendar_disconnected_email(create_user.email, workspace.name, workspace.slug)

        assert reconnect_url in email.call_args.kwargs["body"]
        html_body, mime_type = message.attach_alternative.call_args.args
        assert mime_type == "text/html"
        assert f'href="{reconnect_url}"' in html_body
        assert html_body.count(reconnect_url) == 1
        assert email.call_args.kwargs["body"].count(reconnect_url) == 1

        list_response = session_client.get(reverse("notifications", kwargs={"slug": workspace.slug}))
        assert list_response.status_code == status.HTTP_200_OK
        assert len(list_response.data) == 1
        notice = list_response.data[0]
        assert notice["project"] is None
        assert notice["data"]["google_calendar_connection"]["action_url"] == reconnect_path
        assert notice["data"]["google_calendar_connection"]["status"] == "broken"

        notification = Notification.objects.get(id=notice["id"])
        assert notification.read_at is None
        read_response = session_client.post(
            f"/api/workspaces/{workspace.slug}/users/notifications/{notification.id}/read/",
        )
        assert read_response.status_code == status.HTTP_200_OK
        notification.refresh_from_db()
        assert notification.read_at is not None

        recovered_health_notice = Mock()
        with patch(
            "plane.bgtasks.google_calendar_task.publish_google_calendar_task",
            side_effect=lambda task, *args, **kwargs: recovered_health_notice(task, *args, **kwargs),
        ):
            assert schedule_google_calendar_reconciliations.run() == 1
        recovered_health_notice.assert_called_once()
        assert recovered_health_notice.call_args.args == (
            send_google_calendar_disconnected_email,
            str(connection.id),
            connection.broken_notified_at.isoformat(),
        )
