# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from contextlib import contextmanager
from contextvars import ContextVar

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from plane.bgtasks.google_calendar_task import synchronize_google_calendar_issue
from plane.db.models import Issue, IssueAssignee, IssueLabel
from plane.integrations.google_calendar.dispatch import enqueue_google_calendar_task_on_commit


_google_calendar_issue_signal_suppression_depth = ContextVar(
    "google_calendar_issue_signal_suppression_depth",
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


def dispatch_google_calendar_issue_sync(issue_id):
    """Enqueue convergence for one issue after the current transaction commits."""

    enqueue_google_calendar_task_on_commit(synchronize_google_calendar_issue, str(issue_id))


def _google_calendar_issue_signal_dispatch_is_suppressed():
    return _google_calendar_issue_signal_suppression_depth.get() > 0


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
