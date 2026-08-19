# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import transaction


GOOGLE_CALENDAR_LIFECYCLE_TASK = "plane.bgtasks.google_calendar_task.reconcile_google_calendar_connection"


def enqueue_google_calendar_task_on_commit(task, *args, **kwargs):
    """Publish a Calendar task after the current database transaction commits."""

    def _enqueue_google_calendar_task():
        task.delay(*args, **kwargs)

    transaction.on_commit(_enqueue_google_calendar_task, robust=True)
