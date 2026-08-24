# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from zoneinfo import ZoneInfo

from django.utils import timezone

from plane.db.models import (
    GoogleCalendarConnection,
    IssueAssignee,
    IssueLabel,
    ProjectMember,
    WorkspaceIntegration,
    WorkspaceMember,
)
from plane.db.models.state import StateGroup


def is_google_calendar_project_included(issue, connection):
    """Return whether the issue's project is included for Calendar sync."""

    return (
        issue.project.workspace_id == connection.workspace_integration.workspace_id
        and issue.project.google_calendar_sync_enabled
    )


def _policy_filter_values(policy, plural_key, singular_key):
    values = policy.get(plural_key)
    if values is not None:
        return values

    value = policy.get(singular_key)
    return [] if value is None else [value]


def _is_issue_filter_eligible(issue, policy):
    priorities = set(_policy_filter_values(policy, "priorities", "priority"))
    if priorities and issue.priority not in priorities:
        return False

    selected_label_ids = {str(label_id) for label_id in _policy_filter_values(policy, "label_ids", "label_id")}
    if not selected_label_ids:
        return True

    active_label_ids = {
        str(label_id)
        for label_id in IssueLabel.objects.filter(
            issue_id=issue.id,
            label__deleted_at__isnull=True,
        ).values_list("label_id", flat=True)
    }
    label_match = policy.get("label_match", "any")
    if label_match == "all":
        return selected_label_ids.issubset(active_label_ids)
    if label_match == "any":
        return not selected_label_ids.isdisjoint(active_label_ids)
    return False


def is_issue_assignment_eligible(issue, connection, *, project_inclusion=None):
    """Return whether one issue belongs on one assignee's dedicated calendar."""

    workspace_integration = connection.workspace_integration
    policy = workspace_integration.config or {}
    project_inclusion = project_inclusion or is_google_calendar_project_included

    if not policy.get("enabled", False):
        return False
    mode = policy.get("mode", "assignment")
    if mode not in {"assignment", "filter"}:
        return False
    if issue.workspace_id != workspace_integration.workspace_id:
        return False
    if issue.project.workspace_id != issue.workspace_id or not project_inclusion(issue, connection):
        return False
    if issue.target_date is None:
        return False
    if issue.is_draft or issue.archived_at is not None or issue.deleted_at is not None:
        return False
    if issue.project.archived_at is not None or issue.project.deleted_at is not None:
        return False
    if issue.state is None or issue.state.group == StateGroup.TRIAGE or issue.state.deleted_at is not None:
        return False
    if (
        connection.desired_state != GoogleCalendarConnection.DesiredState.CONNECTED
        or connection.status != GoogleCalendarConnection.Status.ACTIVE
        or not connection.calendar_id
    ):
        return False
    if mode == "filter":
        return _is_issue_filter_eligible(issue, policy)
    return IssueAssignee.objects.filter(issue_id=issue.id, assignee_id=connection.member_id).exists()


def is_issue_open_backfill_eligible(issue, connection, *, project_inclusion=None):
    """Apply assignment eligibility while excluding terminal work items."""

    return is_issue_assignment_eligible(
        issue,
        connection,
        project_inclusion=project_inclusion,
    ) and issue.state.group not in {StateGroup.COMPLETED, StateGroup.CANCELLED}


def _cycle_has_syncable_boundaries(cycle):
    return cycle.start_date is not None and cycle.end_date is not None and cycle.start_date <= cycle.end_date


def is_cycle_sync_enabled(cycle):
    return (
        cycle.deleted_at is None
        and cycle.archived_at is None
        and cycle.project.workspace_id == cycle.workspace_id
        and cycle.project.deleted_at is None
        and cycle.project.archived_at is None
        and cycle.project.google_calendar_sync_enabled
        and _cycle_has_syncable_boundaries(cycle)
    )


def is_cycle_ended(cycle, *, at=None):
    """Return whether the cycle's inclusive local end date is in the past."""

    if not _cycle_has_syncable_boundaries(cycle):
        return False
    at = at or timezone.now()
    cycle_timezone = ZoneInfo(cycle.timezone)
    return cycle.end_date.astimezone(cycle_timezone).date() < at.astimezone(cycle_timezone).date()


def cycle_recipient_connection_ids(cycle, *, healthy_only=True):
    """Resolve Calendar connections for each supported cycle recipient mode."""

    if not is_cycle_sync_enabled(cycle):
        return []
    policy = WorkspaceIntegration.objects.filter(
        workspace_id=cycle.workspace_id,
        integration__provider="google_calendar",
    ).first()
    if policy is None or not (policy.config or {}).get("enabled", False):
        return []

    recipient_mode = (policy.config or {}).get("recipients", "cycle_members")
    if recipient_mode == "cycle_members":
        member_ids = (
            IssueAssignee.objects.filter(
                issue__issue_cycle__cycle_id=cycle.id,
                issue__issue_cycle__deleted_at__isnull=True,
                issue__is_draft=False,
                issue__archived_at__isnull=True,
                issue__deleted_at__isnull=True,
                issue__state__isnull=False,
                issue__state__deleted_at__isnull=True,
            )
            .exclude(issue__state__group=StateGroup.TRIAGE)
            .values_list("assignee_id", flat=True)
        )
    elif recipient_mode == "project_members":
        member_ids = ProjectMember.objects.filter(
            project_id=cycle.project_id,
            is_active=True,
        ).values_list("member_id", flat=True)
    elif recipient_mode == "workspace_members":
        member_ids = WorkspaceMember.objects.filter(
            workspace_id=cycle.workspace_id,
            is_active=True,
        ).values_list("member_id", flat=True)
    else:
        return []

    connections = GoogleCalendarConnection.objects.filter(
        workspace_integration=policy,
        member_id__in=member_ids,
    )
    if healthy_only:
        connections = connections.filter(
            desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
            status=GoogleCalendarConnection.Status.ACTIVE,
        ).exclude(calendar_id="")
    return list(connections.order_by("id").values_list("id", flat=True).distinct())


def is_cycle_creatable(cycle, connection, *, at=None, recipient_connection_ids=None):
    """Return whether a new cycle block may be created for one connection."""

    if cycle.workspace_id != connection.workspace_integration.workspace_id or not is_cycle_sync_enabled(cycle):
        return False
    if not (connection.workspace_integration.config or {}).get("enabled", False):
        return False
    if (
        connection.desired_state != GoogleCalendarConnection.DesiredState.CONNECTED
        or connection.status != GoogleCalendarConnection.Status.ACTIVE
        or not connection.calendar_id
    ):
        return False
    if is_cycle_ended(cycle, at=at):
        return False
    recipient_connection_ids = (
        cycle_recipient_connection_ids(cycle) if recipient_connection_ids is None else recipient_connection_ids
    )
    return connection.id in recipient_connection_ids


def should_retain_cycle_event(cycle, connection, *, at=None, recipient_connection_ids=None):
    """Retain a naturally ended block only while its recipient and project remain eligible."""

    if cycle.workspace_id != connection.workspace_integration.workspace_id or not is_cycle_sync_enabled(cycle):
        return False
    if not (connection.workspace_integration.config or {}).get("enabled", False):
        return False
    recipient_connection_ids = (
        cycle_recipient_connection_ids(cycle, healthy_only=False)
        if recipient_connection_ids is None
        else recipient_connection_ids
    )
    return is_cycle_ended(cycle, at=at) and connection.id in recipient_connection_ids
