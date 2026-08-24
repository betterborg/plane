# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from plane.bgtasks.issue_automation_task import archive_old_issues, close_old_issues
from plane.db.models import Issue
from plane.db.signals import suppress_google_calendar_issue_signal_dispatch
from plane.tests.factories import IssueFactory, ProjectFactory, StateFactory


def _old_issues(project, state):
    with suppress_google_calendar_issue_signal_dispatch():
        issues = [IssueFactory(project=project, state=state) for _ in range(2)]
    Issue.objects.filter(id__in=[issue.id for issue in issues]).update(updated_at=timezone.now() - timedelta(days=31))
    return issues


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarIssueAutomationTask:
    @pytest.mark.parametrize(
        ("task", "project_kwargs", "state_group"),
        [
            (archive_old_issues, {"archive_in": 1}, "completed"),
            (close_old_issues, {"close_in": 1}, "started"),
        ],
    )
    def test_dispatches_persisted_mutations_when_activity_publication_fails(self, task, project_kwargs, state_group):
        project = ProjectFactory(**project_kwargs)
        state = StateFactory(project=project, group=state_group)
        issues = _old_issues(project, state)

        with (
            patch(
                "plane.bgtasks.issue_automation_task.issue_activity.delay",
                side_effect=RuntimeError("broker unavailable"),
            ),
            patch("plane.bgtasks.issue_automation_task.dispatch_google_calendar_issue_syncs") as dispatch,
        ):
            task()

        dispatch.assert_called_once()
        assert set(dispatch.call_args.args[0]) == {issue.id for issue in issues}

    def test_archive_dispatches_every_issue_after_persistence_when_broker_fails(self):
        project = ProjectFactory(archive_in=1)
        completed_state = StateFactory(project=project, group="completed")
        issues = _old_issues(project, completed_state)
        observed_issue_ids = []

        def inspect_archived_issue_then_fail(issue_id):
            issue = Issue.all_objects.get(id=issue_id)
            assert issue.archived_at == timezone.localdate()
            observed_issue_ids.append(issue.id)
            raise RuntimeError("broker unavailable")

        with (
            patch("plane.bgtasks.issue_automation_task.issue_activity.delay"),
            patch(
                "plane.db.signals.synchronize_google_calendar_issue.delay",
                side_effect=inspect_archived_issue_then_fail,
            ),
        ):
            archive_old_issues()

        assert set(observed_issue_ids) == {issue.id for issue in issues}
        for issue in issues:
            issue.refresh_from_db()
            assert issue.archived_at == timezone.localdate()

    def test_close_dispatches_every_issue_after_persistence_when_broker_fails(self):
        project = ProjectFactory(close_in=1)
        open_state = StateFactory(project=project, group="started")
        close_state = StateFactory(project=project, group="cancelled")
        project.default_state = close_state
        project.save(update_fields=["default_state", "updated_at"])
        issues = _old_issues(project, open_state)
        observed_issue_ids = []

        def inspect_closed_issue_then_fail(issue_id):
            issue = Issue.all_objects.get(id=issue_id)
            assert issue.state_id == close_state.id
            observed_issue_ids.append(issue.id)
            raise RuntimeError("broker unavailable")

        with (
            patch("plane.bgtasks.issue_automation_task.issue_activity.delay"),
            patch(
                "plane.db.signals.synchronize_google_calendar_issue.delay",
                side_effect=inspect_closed_issue_then_fail,
            ),
        ):
            close_old_issues()

        assert set(observed_issue_ids) == {issue.id for issue in issues}
        for issue in issues:
            issue.refresh_from_db()
            assert issue.state_id == close_state.id
