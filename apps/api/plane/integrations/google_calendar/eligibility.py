# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from plane.db.models import GoogleCalendarConnection, IssueAssignee
from plane.db.models.state import StateGroup


def is_google_calendar_project_included(issue, connection):
    """Extension point for project-level Calendar selection rules."""

    return issue.project.workspace_id == connection.workspace_integration.workspace_id


def is_issue_assignment_eligible(issue, connection, *, project_inclusion=None):
    """Return whether one issue belongs on one assignee's dedicated calendar."""

    workspace_integration = connection.workspace_integration
    policy = workspace_integration.config or {}
    project_inclusion = project_inclusion or is_google_calendar_project_included

    if not policy.get("enabled", False) or policy.get("mode", "assignment") != "assignment":
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
    return IssueAssignee.objects.filter(issue_id=issue.id, assignee_id=connection.member_id).exists()


def is_issue_open_backfill_eligible(issue, connection, *, project_inclusion=None):
    """Apply assignment eligibility while excluding terminal work items."""

    return is_issue_assignment_eligible(
        issue,
        connection,
        project_inclusion=project_inclusion,
    ) and issue.state.group not in {StateGroup.COMPLETED, StateGroup.CANCELLED}
