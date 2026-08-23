# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import logging
from unittest.mock import Mock, patch

import pytest
from django.db import transaction

from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK,
    enqueue_google_calendar_task_on_commit,
    enqueue_google_calendar_workspace_policy_resyncs_on_commit,
)


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
class TestEnqueueGoogleCalendarTaskOnCommit:
    def test_committed_transaction_publishes_once(self):
        task = Mock()

        with transaction.atomic():
            enqueue_google_calendar_task_on_commit(task, "connection-id", generation=4)
            task.delay.assert_not_called()

        task.delay.assert_called_once_with("connection-id", generation=4)

    def test_rolled_back_transaction_does_not_publish(self):
        task = Mock()

        with pytest.raises(RuntimeError, match="roll back"):
            with transaction.atomic():
                enqueue_google_calendar_task_on_commit(task, "connection-id")
                raise RuntimeError("roll back")

        task.delay.assert_not_called()

    def test_autocommit_publishes_once(self):
        task = Mock()

        enqueue_google_calendar_task_on_commit(task, "connection-id")

        task.delay.assert_called_once_with("connection-id")

    def test_registers_named_robust_callback(self):
        task = Mock()

        with patch("plane.integrations.google_calendar.dispatch.transaction.on_commit") as on_commit:
            enqueue_google_calendar_task_on_commit(task)

        callback = on_commit.call_args.args[0]
        assert callback.__name__ == "_enqueue_google_calendar_task"
        assert callback.__qualname__.endswith(".<locals>._enqueue_google_calendar_task")
        on_commit.assert_called_once_with(callback, robust=True)

    def test_broker_exception_in_autocommit_is_logged_without_propagating(self, caplog):
        failed_task = Mock()
        failed_task.delay.side_effect = RuntimeError("broker unavailable")

        with caplog.at_level(logging.ERROR, logger="django.db.backends.base"):
            enqueue_google_calendar_task_on_commit(failed_task, "connection-id")

        failed_task.delay.assert_called_once_with("connection-id")
        assert "_enqueue_google_calendar_task" in caplog.text
        assert "broker unavailable" in caplog.text

    def test_broker_exception_does_not_prevent_later_callback(self, caplog):
        failed_task = Mock()
        failed_task.delay.side_effect = RuntimeError("broker unavailable")
        later_callback = Mock()

        with caplog.at_level(logging.ERROR, logger="django.db.backends.base"):
            with transaction.atomic():
                enqueue_google_calendar_task_on_commit(failed_task, "connection-id")
                transaction.on_commit(later_callback)

        failed_task.delay.assert_called_once_with("connection-id")
        later_callback.assert_called_once_with()


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
class TestEnqueueGoogleCalendarWorkspacePolicyResyncsOnCommit:
    def test_completion_change_publishes_workspace_resync_only_after_commit(self):
        task = Mock()

        with patch("plane.integrations.google_calendar.dispatch.current_app.signature", return_value=task) as signature:
            with transaction.atomic():
                enqueue_google_calendar_workspace_policy_resyncs_on_commit(
                    "workspace-id",
                    {"update_on_completion": True},
                    {"update_on_completion": False},
                )
                task.delay.assert_not_called()

            task.delay.assert_called_once_with("workspace-id")

        signature.assert_called_once_with(GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK)

    def test_unchanged_completion_behavior_does_not_publish(self):
        with patch("plane.integrations.google_calendar.dispatch.current_app.signature") as signature:
            enqueue_google_calendar_workspace_policy_resyncs_on_commit(
                "workspace-id",
                {},
                {"update_on_completion": True},
            )

        signature.assert_not_called()
