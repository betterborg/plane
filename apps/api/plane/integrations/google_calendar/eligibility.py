# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from plane.db.models import GoogleCalendarConnection, IssueAssignee, IssueLabel
from plane.db.models.state import StateGroup


def is_google_calendar_project_included(issue, connection):
    """Extension point for project-level Calendar selection rules."""

    return issue.project.workspace_id == connection.workspace_integration.workspace_id


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
