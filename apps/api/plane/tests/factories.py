# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from uuid import uuid4

import factory
from django.utils import timezone

from plane.db.models import (
    GoogleCalendarConnection,
    Integration,
    Project,
    ProjectMember,
    User,
    Workspace,
    WorkspaceIntegration,
    WorkspaceMember,
)


class UserFactory(factory.django.DjangoModelFactory):
    """Factory for creating User instances"""

    class Meta:
        model = User
        django_get_or_create = ("email",)

    id = factory.LazyFunction(uuid4)
    email = factory.Sequence(lambda n: f"user{n}@plane.so")
    username = factory.Sequence(lambda n: f"user{n}")
    password = factory.PostGenerationMethodCall("set_password", "password")
    first_name = factory.Sequence(lambda n: f"First{n}")
    last_name = factory.Sequence(lambda n: f"Last{n}")
    is_active = True
    is_superuser = False
    is_staff = False


class WorkspaceFactory(factory.django.DjangoModelFactory):
    """Factory for creating Workspace instances"""

    class Meta:
        model = Workspace
        django_get_or_create = ("slug",)

    id = factory.LazyFunction(uuid4)
    name = factory.Sequence(lambda n: f"Workspace {n}")
    slug = factory.Sequence(lambda n: f"workspace-{n}")
    owner = factory.SubFactory(UserFactory)
    created_at = factory.LazyFunction(timezone.now)
    updated_at = factory.LazyFunction(timezone.now)


class WorkspaceMemberFactory(factory.django.DjangoModelFactory):
    """Factory for creating WorkspaceMember instances"""

    class Meta:
        model = WorkspaceMember

    id = factory.LazyFunction(uuid4)
    workspace = factory.SubFactory(WorkspaceFactory)
    member = factory.SubFactory(UserFactory)
    role = 20  # Admin role by default
    created_at = factory.LazyFunction(timezone.now)
    updated_at = factory.LazyFunction(timezone.now)


class ProjectFactory(factory.django.DjangoModelFactory):
    """Factory for creating Project instances"""

    class Meta:
        model = Project
        django_get_or_create = ("name", "workspace")

    id = factory.LazyFunction(uuid4)
    name = factory.Sequence(lambda n: f"Project {n}")
    workspace = factory.SubFactory(WorkspaceFactory)
    created_by = factory.SelfAttribute("workspace.owner")
    updated_by = factory.SelfAttribute("workspace.owner")
    created_at = factory.LazyFunction(timezone.now)
    updated_at = factory.LazyFunction(timezone.now)


class ProjectMemberFactory(factory.django.DjangoModelFactory):
    """Factory for creating ProjectMember instances"""

    class Meta:
        model = ProjectMember

    id = factory.LazyFunction(uuid4)
    project = factory.SubFactory(ProjectFactory)
    member = factory.SubFactory(UserFactory)
    role = 20  # Admin role by default
    created_at = factory.LazyFunction(timezone.now)
    updated_at = factory.LazyFunction(timezone.now)


class IntegrationFactory(factory.django.DjangoModelFactory):
    """Factory for integration provider records."""

    class Meta:
        model = Integration
        django_get_or_create = ("provider",)

    title = factory.Sequence(lambda n: f"Integration {n}")
    provider = factory.Sequence(lambda n: f"test_provider_{n}")
    description = factory.LazyFunction(dict)


class WorkspaceIntegrationFactory(factory.django.DjangoModelFactory):
    """Factory for workspace integrations that do not require bot ownership."""

    class Meta:
        model = WorkspaceIntegration

    workspace = factory.SubFactory(WorkspaceFactory)
    integration = factory.SubFactory(IntegrationFactory)
    actor = None
    api_token = None


class GoogleCalendarConnectionFactory(factory.django.DjangoModelFactory):
    """Factory for the Calendar connection states consumed by lifecycle tests."""

    class Meta:
        model = GoogleCalendarConnection

    workspace_integration = factory.SubFactory(
        WorkspaceIntegrationFactory,
        integration__title="Google Calendar",
        integration__provider="google_calendar",
    )
    member = factory.SubFactory(UserFactory)

    class Params:
        attempt_only = factory.Trait(
            oauth_state=factory.Sequence(lambda n: f"oauth-state-{n}"),
            oauth_code_verifier="calendar-code-verifier",
            oauth_redirect_uri="https://plane.example/auth/google-calendar/callback",
            oauth_attempt_expires_at=factory.LazyFunction(lambda: timezone.now() + timedelta(minutes=10)),
        )
        bound_broken = factory.Trait(
            provider_account_id=factory.Sequence(lambda n: f"google-account-{n}"),
            provider_email=factory.Sequence(lambda n: f"calendar-user-{n}@example.com"),
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ERROR,
            lifecycle_generation=1,
            last_error="refresh_token_invalid",
        )
        tombstone = factory.Trait(lifecycle_generation=1)
        pending_cleanup = factory.Trait(
            provider_account_id=factory.Sequence(lambda n: f"google-account-{n}"),
            provider_email=factory.Sequence(lambda n: f"calendar-user-{n}@example.com"),
            access_token="encrypted-at-rest-access-token",
            refresh_token="encrypted-at-rest-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
            status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            lifecycle_generation=2,
        )


def google_calendar_connection_scenario(state="absent", **kwargs):
    """Create a named Calendar lifecycle row, or no row for the absent state."""

    if state == "absent":
        return None
    supported_states = {"attempt_only", "bound_broken", "tombstone", "pending_cleanup"}
    if state not in supported_states:
        raise ValueError(f"Unsupported Google Calendar connection state: {state}")
    return GoogleCalendarConnectionFactory(**{state: True}, **kwargs)
