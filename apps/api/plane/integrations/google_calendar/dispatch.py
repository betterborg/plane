# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import logging
import time
from uuid import uuid4

from celery import current_app
from django.db import transaction

from plane.db.models import (
    Cycle,
    GoogleCalendarConnection,
    Issue,
    Label,
    Project,
    State,
    Workspace,
    WorkspaceIntegration,
)
from plane.integrations.google_calendar.telemetry import (
    log_google_calendar_operation,
    publish_google_calendar_analytics,
)
from plane.utils.analytics_events import GOOGLE_CALENDAR_PUBLICATION_FAILURE


GOOGLE_CALENDAR_LIFECYCLE_TASK = "plane.bgtasks.google_calendar_task.reconcile_google_calendar_connection"
GOOGLE_CALENDAR_INVENTORY_TASK = "plane.bgtasks.google_calendar_task.reconcile_google_calendar_inventory"
GOOGLE_CALENDAR_ISSUE_SYNC_TASK = "plane.bgtasks.google_calendar_task.synchronize_google_calendar_issue"
GOOGLE_CALENDAR_OPEN_BACKFILL_TASK = "plane.bgtasks.google_calendar_task.backfill_google_calendar_open_issues"
GOOGLE_CALENDAR_CYCLE_SYNC_TASK = "plane.bgtasks.google_calendar_task.synchronize_google_calendar_cycle"
GOOGLE_CALENDAR_CYCLE_BACKFILL_TASK = "plane.bgtasks.google_calendar_task.backfill_google_calendar_cycles"
GOOGLE_CALENDAR_HEALTH_NOTICE_TASK = "plane.bgtasks.google_calendar_task.send_google_calendar_disconnected_email"
GOOGLE_CALENDAR_STATE_ISSUE_RESYNC_TASK = "plane.bgtasks.google_calendar_task.resync_google_calendar_state_issues"
GOOGLE_CALENDAR_LABEL_ISSUE_RESYNC_TASK = "plane.bgtasks.google_calendar_task.resync_google_calendar_label"
GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK = "plane.bgtasks.google_calendar_task.resync_google_calendar_project_issues"
GOOGLE_CALENDAR_PROJECT_CYCLE_RESYNC_TASK = "plane.bgtasks.google_calendar_task.resync_google_calendar_project_cycles"
GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK = (
    "plane.bgtasks.google_calendar_task.resync_google_calendar_workspace_issues"
)
GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_TASK = (
    "plane.bgtasks.google_calendar_task.resync_google_calendar_workspace_cycles"
)
GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_RECONCILIATION_TASK = (
    "plane.bgtasks.google_calendar_task.reconcile_google_calendar_workspace_issue_resyncs"
)
GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY = "google_calendar_workspace_issue_resync"
GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_METADATA_KEY = "google_calendar_workspace_cycle_resync"
_GOOGLE_CALENDAR_WORKSPACE_POLICY_RESYNC_FIELDS = (
    "update_on_completion",
    "mode",
    "priorities",
    "label_ids",
    "label_match",
)
_GOOGLE_CALENDAR_WORKSPACE_CYCLE_POLICY_RESYNC_FIELDS = ("recipients",)

logger = logging.getLogger(__name__)

_CONNECTION_ARGUMENT_BY_TASK = {
    GOOGLE_CALENDAR_LIFECYCLE_TASK: 0,
    GOOGLE_CALENDAR_INVENTORY_TASK: 0,
    GOOGLE_CALENDAR_OPEN_BACKFILL_TASK: 0,
    GOOGLE_CALENDAR_CYCLE_BACKFILL_TASK: 0,
    GOOGLE_CALENDAR_HEALTH_NOTICE_TASK: 0,
    GOOGLE_CALENDAR_ISSUE_SYNC_TASK: 1,
    GOOGLE_CALENDAR_CYCLE_SYNC_TASK: 1,
}
_ENTITY_MODEL_BY_TASK = {
    GOOGLE_CALENDAR_ISSUE_SYNC_TASK: Issue,
    GOOGLE_CALENDAR_CYCLE_SYNC_TASK: Cycle,
    GOOGLE_CALENDAR_STATE_ISSUE_RESYNC_TASK: State,
    GOOGLE_CALENDAR_LABEL_ISSUE_RESYNC_TASK: Label,
    GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK: Project,
    GOOGLE_CALENDAR_PROJECT_CYCLE_RESYNC_TASK: Project,
}
_WORKSPACE_ARGUMENT_TASKS = {
    GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK,
    GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_TASK,
}
_WORKSPACE_INTEGRATION_ARGUMENT_TASKS = {GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_RECONCILIATION_TASK}


def _task_name(task):
    task_name = getattr(task, "task", None)
    if not isinstance(task_name, str):
        task_name = getattr(task, "name", None)
    return task_name if isinstance(task_name, str) else ""


def _task_publication_fields(task, args):
    """Extract identifiers only from declared positions for one known task."""

    task_name = _task_name(task)
    fields = {"reconciliation_action": task_name.rsplit(".", 1)[-1] or "unknown_task"}
    connection_argument = _CONNECTION_ARGUMENT_BY_TASK.get(task_name)
    if connection_argument is not None and len(args) > connection_argument:
        fields["connection_id"] = args[connection_argument]
    if task_name in _ENTITY_MODEL_BY_TASK and args:
        fields["entity_id"] = args[0]
    if task_name in _WORKSPACE_ARGUMENT_TASKS and args:
        fields["workspace_id"] = args[0]
    return task_name, fields


def _task_publication_failure_context(task_name, args, fields):
    """Enrich a failed publication for analytics without blocking task work."""

    fields = dict(fields)
    analytics_identity = None
    try:
        connection_id = fields.get("connection_id")
        if connection_id is not None:
            connection = (
                GoogleCalendarConnection.all_objects.select_related("workspace_integration__workspace")
                .filter(id=connection_id)
                .first()
            )
            if connection is not None:
                workspace = connection.workspace_integration.workspace
                fields.update(
                    workspace_id=workspace.id,
                    connection_id=connection.id,
                    calendar_generation=connection.calendar_generation,
                )
                analytics_identity = (connection.member_id, workspace.id, workspace.slug)

        entity_model = _ENTITY_MODEL_BY_TASK.get(task_name)
        if entity_model is not None and args:
            if analytics_identity is None:
                entity = entity_model.all_objects.select_related("workspace").filter(id=args[0]).first()
                if entity is not None:
                    workspace = entity.workspace
                    fields["workspace_id"] = workspace.id
                    analytics_identity = (workspace.owner_id, workspace.id, workspace.slug)

        if task_name in _WORKSPACE_ARGUMENT_TASKS and fields.get("workspace_id") is not None:
            workspace = Workspace.objects.filter(id=fields["workspace_id"]).first()
            if workspace is not None:
                fields["workspace_id"] = workspace.id
                analytics_identity = (workspace.owner_id, workspace.id, workspace.slug)

        if task_name in _WORKSPACE_INTEGRATION_ARGUMENT_TASKS and args:
            workspace_integration = (
                WorkspaceIntegration.all_objects.select_related("workspace").filter(id=args[0]).first()
            )
            if workspace_integration is not None:
                workspace = workspace_integration.workspace
                fields["workspace_id"] = workspace.id
                analytics_identity = (workspace.owner_id, workspace.id, workspace.slug)
    except Exception:
        # Failure observability must never hide or replace the broker error.
        pass
    return fields, analytics_identity


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
    if field == "recipients":
        return policy.get(field, "cycle_members")
    raise ValueError(f"Unsupported Google Calendar workspace policy field: {field}")


def publish_google_calendar_task(task, *args, **kwargs):
    """Publish Calendar work immediately and surface broker failures."""

    started_at = time.monotonic()
    task_name, publication_context = _task_publication_fields(task, args)
    try:
        result = task.delay(*args, **kwargs)
    except Exception as exc:
        enqueue_latency_ms = round((time.monotonic() - started_at) * 1000, 3)
        failure_class = type(exc).__name__
        publication_context, analytics_identity = _task_publication_failure_context(
            task_name,
            args,
            publication_context,
        )
        log_google_calendar_operation(
            logger,
            "task_publication",
            outcome="failed",
            attempt=1,
            enqueue_latency_ms=enqueue_latency_ms,
            google_status_class="not_requested",
            publication_failure_class=failure_class,
            **publication_context,
        )
        if analytics_identity is not None:
            user_id, workspace_id, workspace_slug = analytics_identity
            analytics_fields = {key: value for key, value in publication_context.items() if key != "workspace_id"}
            publish_google_calendar_analytics(
                GOOGLE_CALENDAR_PUBLICATION_FAILURE,
                user_id=user_id,
                workspace_id=workspace_id,
                workspace_slug=workspace_slug,
                outcome="failed",
                attempt=1,
                enqueue_latency_ms=enqueue_latency_ms,
                google_status_class="not_requested",
                publication_failure_class=failure_class,
                **analytics_fields,
            )
        raise
    log_google_calendar_operation(
        logger,
        "task_publication",
        outcome="published",
        attempt=1,
        enqueue_latency_ms=round((time.monotonic() - started_at) * 1000, 3),
        google_status_class="not_requested",
        **publication_context,
    )
    return result


def enqueue_google_calendar_task_on_commit(task, *args, **kwargs):
    """Publish a Calendar task after the current database transaction commits."""

    def _enqueue_google_calendar_task():
        try:
            publish_google_calendar_task(task, *args, **kwargs)
        except Exception:
            # The publisher already records only the safe failure class and
            # identifiers. Do not let Django's robust-callback logger serialize
            # a broker exception that may contain credentials or task content.
            return False
        return True

    transaction.on_commit(_enqueue_google_calendar_task, robust=True)


def enqueue_google_calendar_project_resync_on_commit(project_id):
    """Request project work-item convergence after the current transaction commits."""

    project_issue_resync_task = current_app.signature(GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK)
    enqueue_google_calendar_task_on_commit(project_issue_resync_task, str(project_id))


def enqueue_google_calendar_workspace_policy_resyncs_on_commit(
    workspace_integration,
    previous_policy,
    current_policy,
):
    """Durably request after-commit convergence for a workspace policy change."""

    issue_resync_required = any(
        _workspace_policy_resync_value(previous_policy, field) != _workspace_policy_resync_value(current_policy, field)
        for field in _GOOGLE_CALENDAR_WORKSPACE_POLICY_RESYNC_FIELDS
    )
    cycle_resync_required = any(
        _workspace_policy_resync_value(previous_policy, field) != _workspace_policy_resync_value(current_policy, field)
        for field in _GOOGLE_CALENDAR_WORKSPACE_CYCLE_POLICY_RESYNC_FIELDS
    )
    if not issue_resync_required and not cycle_resync_required:
        return None

    generation = str(uuid4())
    metadata = dict(workspace_integration.metadata or {})
    if issue_resync_required:
        metadata[GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY] = generation
    if cycle_resync_required:
        metadata[GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_METADATA_KEY] = generation
    workspace_integration.metadata = metadata
    workspace_integration.save(update_fields=["metadata", "updated_at"])

    if issue_resync_required:
        workspace_issue_resync_task = current_app.signature(GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK)
        enqueue_google_calendar_task_on_commit(
            workspace_issue_resync_task,
            str(workspace_integration.workspace_id),
            policy_generation=generation,
        )
    if cycle_resync_required:
        workspace_cycle_resync_task = current_app.signature(GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_TASK)
        enqueue_google_calendar_task_on_commit(
            workspace_cycle_resync_task,
            str(workspace_integration.workspace_id),
            policy_generation=generation,
        )
    return generation
