# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import logging
from unittest.mock import Mock, patch

import pytest
from django.db import transaction

from plane.tests.factories import ProjectFactory


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
class TestSoftDeleteModel:
    def test_committed_soft_delete_publishes_recursive_deletion_after_commit(self):
        project = ProjectFactory()

        with patch("plane.db.mixins.soft_delete_related_objects.delay") as delay:
            with transaction.atomic():
                project.delete()
                delay.assert_not_called()

            delay.assert_called_once_with("db", "project", project.pk, using=None)

    def test_rolled_back_soft_delete_does_not_publish_recursive_deletion(self):
        project = ProjectFactory()

        with patch("plane.db.mixins.soft_delete_related_objects.delay") as delay:
            with pytest.raises(RuntimeError, match="roll back"):
                with transaction.atomic():
                    project.delete()
                    raise RuntimeError("roll back")

            delay.assert_not_called()

        project.refresh_from_db()
        assert project.deleted_at is None

    def test_broker_failure_is_logged_without_reversing_soft_delete(self, caplog):
        project = ProjectFactory()

        with (
            patch(
                "plane.db.mixins.soft_delete_related_objects.delay",
                side_effect=RuntimeError("broker unavailable"),
            ) as delay,
            caplog.at_level(logging.ERROR, logger="django.db.backends.base"),
        ):
            with transaction.atomic():
                project.delete()

        delay.assert_called_once_with("db", "project", project.pk, using=None)
        project.refresh_from_db()
        assert project.deleted_at is not None
        assert "_publish_soft_delete_related_objects" in caplog.text
        assert "broker unavailable" in caplog.text

    def test_broker_failure_does_not_prevent_later_robust_callback(self, caplog):
        project = ProjectFactory()
        later_callback = Mock()

        with (
            patch(
                "plane.db.mixins.soft_delete_related_objects.delay",
                side_effect=RuntimeError("broker unavailable"),
            ),
            caplog.at_level(logging.ERROR, logger="django.db.backends.base"),
        ):
            with transaction.atomic():
                project.delete()
                transaction.on_commit(later_callback)

        later_callback.assert_called_once_with()

    def test_registers_named_robust_callback(self):
        project = ProjectFactory()

        with patch("plane.db.mixins.transaction.on_commit") as on_commit:
            project.delete()

        callback = on_commit.call_args.args[0]
        assert callback.__name__ == "_publish_soft_delete_related_objects"
        assert callback.__qualname__.endswith(".<locals>._publish_soft_delete_related_objects")
        on_commit.assert_called_once_with(callback, robust=True)
