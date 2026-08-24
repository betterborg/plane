# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

from celery import current_app
from django.db import transaction


GOOGLE_CALENDAR_LIFECYCLE_TASK = "plane.bgtasks.google_calendar_task.reconcile_google_calendar_connection"
GOOGLE_CALENDAR_ISSUE_SYNC_TASK = "plane.bgtasks.google_calendar_task.synchronize_google_calendar_issue"
GOOGLE_CALENDAR_OPEN_BACKFILL_TASK = "plane.bgtasks.google_calendar_task.backfill_google_calendar_open_issues"
GOOGLE_CALENDAR_STATE_ISSUE_RESYNC_TASK = "plane.bgtasks.google_calendar_task.resync_google_calendar_state_issues"
GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK = (
    "plane.bgtasks.google_calendar_task.resync_google_calendar_workspace_issues"
)
GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_RECONCILIATION_TASK = (
    "plane.bgtasks.google_calendar_task.reconcile_google_calendar_workspace_issue_resyncs"
)
GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY = "google_calendar_workspace_issue_resync"
_GOOGLE_CALENDAR_WORKSPACE_POLICY_RESYNC_FIELDS = (
    "update_on_completion",
    "mode",
    "priorities",
    "label_ids",
    "label_match",
)


def _workspace_policy_resync_value(policy, field):
    if field == "update_on_completion":
        return policy.get(field, True)
    if field == "mode":
        return policy.get(field, "assignment")
    if field == "priorities":
        priorities = policy.get(field)
        if priorities is None:
            priority = policy.get("priority")
            priorities = [] if priority is None else [priority]
        return frozenset(priorities)
    if field == "label_ids":
        label_ids = policy.get(field)
        if label_ids is None:
            label_id = policy.get("label_id")
            label_ids = [] if label_id is None else [label_id]
        return frozenset(str(label_id) for label_id in label_ids)
    if field == "label_match":
        return policy.get(field, "any")
    raise ValueError(f"Unsupported Google Calendar workspace policy field: {field}")


def publish_google_calendar_task(task, *args, **kwargs):
    """Publish Calendar work immediately and surface broker failures."""

    return task.delay(*args, **kwargs)


def enqueue_google_calendar_task_on_commit(task, *args, **kwargs):
    """Publish a Calendar task after the current database transaction commits."""

    def _enqueue_google_calendar_task():
        publish_google_calendar_task(task, *args, **kwargs)

    transaction.on_commit(_enqueue_google_calendar_task, robust=True)


def enqueue_google_calendar_workspace_policy_resyncs_on_commit(
    workspace_integration,
    previous_policy,
    current_policy,
):
    """Durably request after-commit convergence for a workspace policy change."""

    if all(
        _workspace_policy_resync_value(previous_policy, field) == _workspace_policy_resync_value(current_policy, field)
        for field in _GOOGLE_CALENDAR_WORKSPACE_POLICY_RESYNC_FIELDS
    ):
        return None

    generation = str(uuid4())
    metadata = dict(workspace_integration.metadata or {})
    metadata[GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY] = generation
    workspace_integration.metadata = metadata
    workspace_integration.save(update_fields=["metadata", "updated_at"])

    workspace_issue_resync_task = current_app.signature(GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK)
    enqueue_google_calendar_task_on_commit(
        workspace_issue_resync_task,
        str(workspace_integration.workspace_id),
        policy_generation=generation,
    )
    return generation
