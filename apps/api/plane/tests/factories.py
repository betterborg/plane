# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from uuid import uuid4

import factory
from django.utils import timezone

from plane.db.models import (
    Cycle,
    CycleIssue,
    GoogleCalendarConnection,
    GoogleCalendarEvent,
    Integration,
    Issue,
    IssueAssignee,
    IssueLabel,
    Label,
    Project,
    ProjectMember,
    State,
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
    identifier = factory.Sequence(lambda n: f"PRJ{n}")
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
        active = factory.Trait(
            provider_account_id=factory.Sequence(lambda n: f"google-account-{n}"),
            provider_email=factory.Sequence(lambda n: f"calendar-user-{n}@example.com"),
            calendar_id=factory.Sequence(lambda n: f"plane-calendar-{n}"),
            refresh_token="encrypted-at-rest-refresh-token",
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
            lifecycle_generation=1,
        )


class StateFactory(factory.django.DjangoModelFactory):
    """Factory for project states used by Calendar work-item scenarios."""

    class Meta:
        model = State

    project = factory.SubFactory(ProjectFactory)
    name = factory.Sequence(lambda n: f"State {n}")
    color = "#60646C"
    group = "unstarted"


class LabelFactory(factory.django.DjangoModelFactory):
    """Factory for labels included in Calendar event descriptions."""

    class Meta:
        model = Label

    project = factory.SubFactory(ProjectFactory)
    workspace = factory.SelfAttribute("project.workspace")
    name = factory.Sequence(lambda n: f"Label {n}")
    color = "#60646C"


class IssueFactory(factory.django.DjangoModelFactory):
    """Factory for due-dated work items synchronized with Calendar."""

    class Meta:
        model = Issue

    project = factory.SubFactory(ProjectFactory)
    state = factory.SubFactory(StateFactory, project=factory.SelfAttribute("..project"))
    name = factory.Sequence(lambda n: f"Work item {n}")
    target_date = factory.LazyFunction(lambda: timezone.localdate() + timedelta(days=7))


class IssueAssigneeFactory(factory.django.DjangoModelFactory):
    """Factory for assignment-mode Calendar eligibility."""

    class Meta:
        model = IssueAssignee

    issue = factory.SubFactory(IssueFactory)
    assignee = factory.SubFactory(UserFactory)
    project = factory.SelfAttribute("issue.project")


class IssueLabelFactory(factory.django.DjangoModelFactory):
    """Factory for labels attached to Calendar work items."""

    class Meta:
        model = IssueLabel

    issue = factory.SubFactory(IssueFactory)
    label = factory.SubFactory(
        LabelFactory,
        project=factory.SelfAttribute("..issue.project"),
    )
    project = factory.SelfAttribute("issue.project")


class CycleFactory(factory.django.DjangoModelFactory):
    """Factory for timezone-snapshotted cycles synchronized with Calendar."""

    class Meta:
        model = Cycle

    project = factory.SubFactory(ProjectFactory)
    owned_by = factory.SelfAttribute("project.workspace.owner")
    name = factory.Sequence(lambda n: f"Cycle {n}")
    timezone = factory.SelfAttribute("project.timezone")
    start_date = factory.LazyFunction(lambda: timezone.now() - timedelta(days=1))
    end_date = factory.LazyFunction(lambda: timezone.now() + timedelta(days=7))


class CycleIssueFactory(factory.django.DjangoModelFactory):
    """Factory for work-item membership in a Calendar cycle."""

    class Meta:
        model = CycleIssue

    cycle = factory.SubFactory(CycleFactory)
    issue = factory.SubFactory(IssueFactory, project=factory.SelfAttribute("..cycle.project"))
    project = factory.SelfAttribute("cycle.project")


class GoogleCalendarEventFactory(factory.django.DjangoModelFactory):
    """Factory for durable Calendar provider correlations."""

    class Meta:
        model = GoogleCalendarEvent

    connection = factory.SubFactory(GoogleCalendarConnectionFactory, active=True)
    entity_type = GoogleCalendarEvent.EntityType.WORK_ITEM
    entity_id = factory.LazyFunction(uuid4)
    google_event_id = factory.Sequence(lambda n: f"planeevent{n}")
    payload_hash = "0" * 64


def google_calendar_connection_scenario(state="absent", **kwargs):
    """Create a named Calendar lifecycle row, or no row for the absent state."""

    if state == "absent":
        return None
    supported_states = {"attempt_only", "bound_broken", "tombstone", "pending_cleanup", "active"}
    if state not in supported_states:
        raise ValueError(f"Unsupported Google Calendar connection state: {state}")
    return GoogleCalendarConnectionFactory(**{state: True}, **kwargs)
