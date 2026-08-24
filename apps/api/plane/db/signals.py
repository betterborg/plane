# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from contextlib import contextmanager
from contextvars import ContextVar

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from plane.bgtasks.google_calendar_task import (
    paced_google_calendar_cycle_sync_tasks,
    paced_google_calendar_issue_sync_tasks,
    resync_google_calendar_label,
    resync_google_calendar_state_issues,
    synchronize_google_calendar_cycle,
    synchronize_google_calendar_issue,
)
from plane.db.models import Cycle, CycleIssue, Issue, IssueAssignee, IssueLabel, Label, State
from plane.integrations.google_calendar.dispatch import enqueue_google_calendar_task_on_commit


_google_calendar_issue_signal_suppression_depth = ContextVar(
    "google_calendar_issue_signal_suppression_depth",
    default=0,
)
_google_calendar_cycle_signal_suppression_depth = ContextVar(
    "google_calendar_cycle_signal_suppression_depth",
    default=0,
)


@contextmanager
def suppress_google_calendar_issue_signal_dispatch():
    """Temporarily leave Calendar issue dispatch to an owning mutation boundary."""

    depth = _google_calendar_issue_signal_suppression_depth.get()
    token = _google_calendar_issue_signal_suppression_depth.set(depth + 1)
    try:
        yield
    finally:
        _google_calendar_issue_signal_suppression_depth.reset(token)


@contextmanager
def suppress_google_calendar_cycle_signal_dispatch():
    """Temporarily leave Calendar cycle dispatch to an owning mutation boundary."""

    depth = _google_calendar_cycle_signal_suppression_depth.get()
    token = _google_calendar_cycle_signal_suppression_depth.set(depth + 1)
    try:
        yield
    finally:
        _google_calendar_cycle_signal_suppression_depth.reset(token)


def dispatch_google_calendar_issue_sync(issue_id):
    """Enqueue convergence for one issue and its current cycle after commit."""

    dispatch_google_calendar_issue_syncs([issue_id])


def dispatch_google_calendar_issue_syncs(issue_ids):
    """Enqueue affected issues and cycles with one pacing sequence per connection."""

    unique_issue_ids = list(dict.fromkeys(str(issue_id) for issue_id in issue_ids))
    paced_issue_tasks = list(paced_google_calendar_issue_sync_tasks(unique_issue_ids))
    targeted_issue_ids = set()
    next_countdown_by_connection = {}
    for sync_task, issue_id, connection_id in paced_issue_tasks:
        targeted_issue_ids.add(issue_id)
        countdown = sync_task.options["countdown"]
        next_countdown_by_connection[connection_id] = countdown + 1
        enqueue_google_calendar_task_on_commit(sync_task, issue_id, connection_id)
    for issue_id in unique_issue_ids:
        if issue_id not in targeted_issue_ids:
            enqueue_google_calendar_task_on_commit(synchronize_google_calendar_issue, issue_id)

    cycle_ids = list(
        CycleIssue.objects.filter(issue_id__in=unique_issue_ids)
        .order_by()
        .values_list("cycle_id", flat=True)
        .distinct()
    )
    paced_cycle_tasks = list(paced_google_calendar_cycle_sync_tasks(cycle_ids))
    targeted_cycle_ids = set()
    for sync_task, cycle_id, connection_id in paced_cycle_tasks:
        targeted_cycle_ids.add(cycle_id)
        countdown = next_countdown_by_connection.get(connection_id, 0) + sync_task.options["countdown"]
        enqueue_google_calendar_task_on_commit(
            sync_task.set(countdown=countdown),
            cycle_id,
            connection_id,
        )
    for cycle_id in cycle_ids:
        if str(cycle_id) not in targeted_cycle_ids:
            enqueue_google_calendar_task_on_commit(synchronize_google_calendar_cycle, str(cycle_id))


def dispatch_google_calendar_cycle_sync(cycle_id, countdown=0):
    """Enqueue convergence for one cycle after the current transaction commits."""

    cycle_tasks = list(paced_google_calendar_cycle_sync_tasks([cycle_id]))
    if cycle_tasks:
        for sync_task, target_cycle_id, connection_id in cycle_tasks:
            sync_task = sync_task.set(countdown=countdown)
            enqueue_google_calendar_task_on_commit(sync_task, target_cycle_id, connection_id)
    else:
        enqueue_google_calendar_task_on_commit(synchronize_google_calendar_cycle, str(cycle_id))


def dispatch_google_calendar_cycle_syncs(cycle_ids):
    """Enqueue each affected cycle once after the current transaction commits."""

    unique_cycle_ids = list(dict.fromkeys(str(cycle_id) for cycle_id in cycle_ids))
    paced_tasks = list(paced_google_calendar_cycle_sync_tasks(unique_cycle_ids))
    targeted_cycle_ids = set()
    for sync_task, cycle_id, connection_id in paced_tasks:
        targeted_cycle_ids.add(cycle_id)
        enqueue_google_calendar_task_on_commit(sync_task, cycle_id, connection_id)
    for cycle_id in unique_cycle_ids:
        if cycle_id not in targeted_cycle_ids:
            enqueue_google_calendar_task_on_commit(synchronize_google_calendar_cycle, cycle_id)


def _google_calendar_issue_signal_dispatch_is_suppressed():
    return _google_calendar_issue_signal_suppression_depth.get() > 0


def _google_calendar_cycle_signal_dispatch_is_suppressed():
    return _google_calendar_cycle_signal_suppression_depth.get() > 0


@receiver(post_save, sender=Issue, dispatch_uid="google_calendar_issue_post_save")
@receiver(post_delete, sender=Issue, dispatch_uid="google_calendar_issue_post_delete")
def dispatch_google_calendar_issue_model_signal(sender, instance, **kwargs):
    if not _google_calendar_issue_signal_dispatch_is_suppressed():
        dispatch_google_calendar_issue_sync(instance.id)


@receiver(post_save, sender=IssueAssignee, dispatch_uid="google_calendar_issue_assignee_post_save")
@receiver(post_delete, sender=IssueAssignee, dispatch_uid="google_calendar_issue_assignee_post_delete")
@receiver(post_save, sender=IssueLabel, dispatch_uid="google_calendar_issue_label_post_save")
@receiver(post_delete, sender=IssueLabel, dispatch_uid="google_calendar_issue_label_post_delete")
def dispatch_google_calendar_issue_relation_signal(sender, instance, **kwargs):
    if not _google_calendar_issue_signal_dispatch_is_suppressed():
        dispatch_google_calendar_issue_sync(instance.issue_id)


@receiver(post_save, sender=State, dispatch_uid="google_calendar_state_post_save")
def dispatch_google_calendar_state_model_signal(sender, instance, created, **kwargs):
    """Resync issues only when an existing State definition is saved."""

    if not created:
        enqueue_google_calendar_task_on_commit(resync_google_calendar_state_issues, str(instance.id))


@receiver(post_save, sender=Label, dispatch_uid="google_calendar_label_post_save")
def dispatch_google_calendar_label_model_signal(sender, instance, created, **kwargs):
    """Resync linked issues only when an existing Label definition is saved."""

    if not created:
        enqueue_google_calendar_task_on_commit(resync_google_calendar_label, str(instance.id))


@receiver(post_save, sender=Cycle, dispatch_uid="google_calendar_cycle_post_save")
@receiver(post_delete, sender=Cycle, dispatch_uid="google_calendar_cycle_post_delete")
def dispatch_google_calendar_cycle_model_signal(sender, instance, **kwargs):
    """Converge an existing cycle after a lifecycle mutation is durable."""

    if not kwargs.get("created", False) and not _google_calendar_cycle_signal_dispatch_is_suppressed():
        dispatch_google_calendar_cycle_sync(instance.id)
