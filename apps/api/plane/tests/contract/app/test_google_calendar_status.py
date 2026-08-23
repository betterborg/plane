# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.urls import reverse
from rest_framework import status

from plane.db.models import GoogleCalendarConnection, WorkspaceIntegration, WorkspaceMember
from plane.tests.factories import (
    GoogleCalendarConnectionFactory,
    IntegrationFactory,
    UserFactory,
    WorkspaceIntegrationFactory,
)


@pytest.fixture
def calendar_workspace_integration(db, workspace):
    return WorkspaceIntegrationFactory(
        workspace=workspace,
        integration=IntegrationFactory(title="Google Calendar", provider="google_calendar"),
        config={
            "enabled": True,
            "mode": "filter",
            "update_on_completion": False,
            "recipients": "cycle_members",
            "label_id": None,
            "priority": "urgent",
            "provider_secret": "must-not-serialize",
        },
    )


def _status_url(workspace):
    return reverse("google-calendar-workspace-status", kwargs={"slug": workspace.slug})


def _roster_url(workspace):
    return reverse("google-calendar-connection-roster", kwargs={"slug": workspace.slug})


def _set_requesting_member(session_client, workspace, user, *, role, is_active=True):
    WorkspaceMember.objects.update_or_create(
        workspace=workspace,
        member=user,
        defaults={"role": role, "is_active": is_active},
    )
    session_client.force_authenticate(user=user)


@pytest.mark.contract
class TestGoogleCalendarWorkspaceStatus:
    @pytest.mark.django_db
    def test_unreleased_status_and_roster_are_not_found(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
    ):
        with override_settings(GOOGLE_CALENDAR_RELEASED=False):
            status_response = session_client.get(_status_url(workspace))
            roster_response = session_client.get(_roster_url(workspace))

        assert status_response.status_code == status.HTTP_404_NOT_FOUND
        assert roster_response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.django_db
    @pytest.mark.parametrize("role", (20, 15, 5))
    def test_active_members_can_read_only_their_status_and_public_policy(
        self,
        session_client,
        workspace,
        create_user,
        calendar_workspace_integration,
        role,
    ):
        _set_requesting_member(session_client, workspace, create_user, role=role)
        connection = GoogleCalendarConnectionFactory(
            workspace_integration=calendar_workspace_integration,
            member=create_user,
            provider_account_id="requester-google-account",
            provider_email="requester@example.com",
            calendar_id="requester-calendar-id",
            access_token="requester-access-token",
            refresh_token="requester-refresh-token",
            scopes=["secret-scope"],
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
            lifecycle_generation=7,
            oauth_state="requester-oauth-state",
            oauth_code_verifier="requester-code-verifier",
            last_error="private-error",
        )
        other_member = UserFactory()
        WorkspaceMember.objects.create(workspace=workspace, member=other_member, role=15)
        GoogleCalendarConnectionFactory(
            workspace_integration=calendar_workspace_integration,
            member=other_member,
            status=GoogleCalendarConnection.Status.ERROR,
            provider_email="other-member@example.com",
        )

        with (
            override_settings(GOOGLE_CALENDAR_RELEASED=True),
            patch("plane.app.views.integration._has_complete_google_calendar_credentials", return_value=True),
        ):
            response = session_client.get(_status_url(workspace))

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {
            "available": True,
            "policy": {
                "enabled": True,
                "mode": "filter",
                "update_on_completion": False,
                "recipients": "cycle_members",
                "label_id": None,
                "priority": "urgent",
            },
            "connection": {"status": "healthy"},
        }
        assert str(connection.id) not in str(response.data)
        assert "other-member@example.com" not in str(response.data)

    @pytest.mark.django_db
    @pytest.mark.parametrize(
        ("internal_status", "public_connection"),
        (
            (GoogleCalendarConnection.Status.PENDING, {"status": "provisioning"}),
            (GoogleCalendarConnection.Status.ACTIVE, {"status": "healthy"}),
            (GoogleCalendarConnection.Status.ERROR, {"status": "broken"}),
            (GoogleCalendarConnection.Status.CLEANUP_PENDING, {"status": "disconnecting"}),
            (GoogleCalendarConnection.Status.DISCONNECTED, None),
        ),
    )
    def test_status_maps_only_public_lifecycle_states(
        self,
        session_client,
        workspace,
        create_user,
        calendar_workspace_integration,
        internal_status,
        public_connection,
    ):
        GoogleCalendarConnectionFactory(
            workspace_integration=calendar_workspace_integration,
            member=create_user,
            status=internal_status,
            oauth_state="private-oauth-attempt",
            lifecycle_generation=9,
        )

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.get(_status_url(workspace))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["connection"] == public_connection
        assert "private-oauth-attempt" not in str(response.data)

    @pytest.mark.django_db
    @pytest.mark.parametrize("membership", ("inactive", "outsider"))
    def test_inactive_members_and_outsiders_cannot_read_status(
        self,
        session_client,
        workspace,
        create_user,
        calendar_workspace_integration,
        membership,
    ):
        if membership == "inactive":
            WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(is_active=False)
        else:
            WorkspaceMember.objects.filter(workspace=workspace, member=create_user).delete()

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.get(_status_url(workspace))

        assert response.status_code == status.HTTP_403_FORBIDDEN

    @pytest.mark.django_db
    def test_status_without_workspace_setup_returns_public_defaults(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
    ):
        WorkspaceIntegration.objects.filter(id=calendar_workspace_integration.id).delete()

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.get(_status_url(workspace))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["policy"] == {
            "enabled": False,
            "mode": "assignment",
            "update_on_completion": True,
            "recipients": "cycle_members",
            "label_id": None,
            "priority": None,
        }
        assert response.data["connection"] is None


@pytest.mark.contract
class TestGoogleCalendarConnectionRoster:
    @pytest.mark.django_db
    def test_role_20_roster_names_members_and_exposes_only_public_states(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
    ):
        expected = []
        for index, (internal_status, public_status) in enumerate(
            (
                (GoogleCalendarConnection.Status.PENDING, "provisioning"),
                (GoogleCalendarConnection.Status.ACTIVE, "healthy"),
                (GoogleCalendarConnection.Status.ERROR, "broken"),
                (GoogleCalendarConnection.Status.CLEANUP_PENDING, "disconnecting"),
            )
        ):
            member = UserFactory(display_name=f"Calendar member {index}")
            WorkspaceMember.objects.create(workspace=workspace, member=member, role=15)
            last_success_at = None if index == 0 else datetime(2026, 8, 23, 12, index, tzinfo=UTC)
            GoogleCalendarConnectionFactory(
                workspace_integration=calendar_workspace_integration,
                member=member,
                provider_account_id=f"google-account-{index}",
                provider_email=f"google-{index}@example.com",
                calendar_id=f"private-calendar-{index}",
                access_token=f"private-access-{index}",
                refresh_token=f"private-refresh-{index}",
                scopes=["private-scope"],
                status=internal_status,
                lifecycle_generation=index + 1,
                last_success_at=last_success_at,
                oauth_state=f"private-state-{index}",
                oauth_code_verifier=f"private-verifier-{index}",
                last_error=f"private-error-{index}",
            )
            expected.append(
                {
                    "member": {
                        "id": str(member.id),
                        "first_name": member.first_name,
                        "last_name": member.last_name,
                        "avatar": member.avatar,
                        "avatar_url": member.avatar_url,
                        "is_bot": member.is_bot,
                        "display_name": member.display_name,
                    },
                    "connection": {
                        "status": public_status,
                        "last_success_at": None if last_success_at is None else f"2026-08-23T12:0{index}:00Z",
                    },
                }
            )

        tombstone_member = UserFactory(display_name="Disconnected member")
        WorkspaceMember.objects.create(workspace=workspace, member=tombstone_member, role=15)
        GoogleCalendarConnectionFactory(
            workspace_integration=calendar_workspace_integration,
            member=tombstone_member,
            status=GoogleCalendarConnection.Status.DISCONNECTED,
            lifecycle_generation=12,
        )
        inactive_member = UserFactory(display_name="Inactive member")
        WorkspaceMember.objects.create(workspace=workspace, member=inactive_member, role=15, is_active=False)
        GoogleCalendarConnectionFactory(
            workspace_integration=calendar_workspace_integration,
            member=inactive_member,
            status=GoogleCalendarConnection.Status.ACTIVE,
        )

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.get(_roster_url(workspace))

        assert response.status_code == status.HTTP_200_OK
        assert response.data == expected
        serialized = str(response.data)
        for private_value in (
            "google-account-",
            "@example.com",
            "private-calendar-",
            "private-access-",
            "private-refresh-",
            "private-scope",
            "private-state-",
            "private-verifier-",
            "private-error-",
            "Disconnected member",
            "Inactive member",
        ):
            assert private_value not in serialized

    @pytest.mark.django_db
    def test_roster_excludes_former_members_and_does_not_duplicate_rejoined_members(
        self,
        session_client,
        workspace,
        calendar_workspace_integration,
    ):
        former_member = UserFactory(display_name="Former member")
        former_membership = WorkspaceMember.objects.create(workspace=workspace, member=former_member, role=15)
        WorkspaceMember.objects.filter(id=former_membership.id).delete()
        GoogleCalendarConnectionFactory(
            workspace_integration=calendar_workspace_integration,
            member=former_member,
            status=GoogleCalendarConnection.Status.ACTIVE,
        )

        rejoined_member = UserFactory(display_name="Rejoined member")
        previous_membership = WorkspaceMember.objects.create(workspace=workspace, member=rejoined_member, role=15)
        WorkspaceMember.objects.filter(id=previous_membership.id).delete()
        WorkspaceMember.objects.create(workspace=workspace, member=rejoined_member, role=15)
        GoogleCalendarConnectionFactory(
            workspace_integration=calendar_workspace_integration,
            member=rejoined_member,
            status=GoogleCalendarConnection.Status.ACTIVE,
        )

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.get(_roster_url(workspace))

        assert response.status_code == status.HTTP_200_OK
        member_names = [entry["member"]["display_name"] for entry in response.data]
        assert "Former member" not in member_names
        assert member_names.count("Rejoined member") == 1

    @pytest.mark.django_db
    @pytest.mark.parametrize("membership", ("role_15", "role_5", "inactive", "outsider"))
    def test_only_active_role_20_can_read_roster(
        self,
        session_client,
        workspace,
        create_user,
        calendar_workspace_integration,
        membership,
    ):
        if membership == "outsider":
            WorkspaceMember.objects.filter(workspace=workspace, member=create_user).delete()
        else:
            role = 15 if membership in {"role_15", "inactive"} else 5
            WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(
                role=role,
                is_active=membership != "inactive",
            )

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.get(_roster_url(workspace))

        assert response.status_code == status.HTTP_403_FORBIDDEN
