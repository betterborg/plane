# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import Mock


def targeted_task(side_effect=None):
    """Build a task mock that preserves Celery Signature options."""

    task = Mock(options={})

    def set_options(**options):
        task.options.update(options)
        return task

    task.set.side_effect = set_options
    if side_effect is not None:
        task.delay.side_effect = side_effect
    return task
