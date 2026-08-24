# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

from celery import current_app
from django.db import transaction


GOOGLE_CALENDAR_LIFECYCLE_TASK = "plane.bgtasks.google_calendar_task.reconcile_google_calendar_connection"
GOOGLE_CALENDAR_ISSUE_SYNC_TASK = "plane.bgtasks.google_calendar_task.synchronize_google_calendar_issue"
GOOGLE_CALENDAR_OPEN_BACKFILL_TASK = "plane.bgtasks.google_calendar_task.backfill_google_calendar_open_issues"
GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK = (
    "plane.bgtasks.google_calendar_task.resync_google_calendar_workspace_issues"
)
GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_RECONCILIATION_TASK = (
    "plane.bgtasks.google_calendar_task.reconcile_google_calendar_workspace_issue_resyncs"
)
GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY = "google_calendar_workspace_issue_resync"


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

    previous_update_on_completion = previous_policy.get("update_on_completion", True)
    current_update_on_completion = current_policy.get("update_on_completion", True)
    if previous_update_on_completion == current_update_on_completion:
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
