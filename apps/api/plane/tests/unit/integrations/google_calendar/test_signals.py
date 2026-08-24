# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import call, patch

import pytest

from plane.db.signals import (
    dispatch_google_calendar_cycle_syncs,
    dispatch_google_calendar_issue_syncs,
    suppress_google_calendar_issue_signal_dispatch,
)
from plane.tests.factories import (
    CycleFactory,
    CycleIssueFactory,
    GoogleCalendarConnectionFactory,
    IssueAssigneeFactory,
    IssueFactory,
    IssueLabelFactory,
    LabelFactory,
    StateFactory,
    UserFactory,
    WorkspaceIntegrationFactory,
)


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarIssueSignals:
    def test_multi_issue_dispatch_paces_each_connection_independently(self):
        workspace_integration = WorkspaceIntegrationFactory(
            integration__provider="google_calendar",
            config={"enabled": True, "mode": "assignment", "recipients": "cycle_members"},
        )
        first_connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            active=True,
        )
        second_connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            member=UserFactory(),
            active=True,
        )
        issues = [IssueFactory(project__workspace=workspace_integration.workspace) for _ in range(3)]
        for issue in issues:
            for connection in (first_connection, second_connection):
                IssueAssigneeFactory(issue=issue, assignee=connection.member, project=issue.project)
            CycleIssueFactory(
                cycle=CycleFactory(project=issue.project),
                issue=issue,
                project=issue.project,
            )

        with patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            dispatch_google_calendar_issue_syncs(issue.id for issue in issues)

        countdowns_by_connection = {}
        for queued in enqueue.call_args_list:
            task, _, connection_id = queued.args
            countdowns_by_connection.setdefault(connection_id, []).append(task.options["countdown"])
        assert countdowns_by_connection == {
            str(first_connection.id): [0, 1, 2, 3, 4, 5],
            str(second_connection.id): [0, 1, 2, 3, 4, 5],
        }

    def test_issue_dispatch_targets_each_connection_with_its_own_pacing_sequence(self):
        workspace_integration = WorkspaceIntegrationFactory(
            integration__provider="google_calendar",
            config={"enabled": True, "mode": "assignment"},
        )
        issue = IssueFactory(project__workspace=workspace_integration.workspace)
        first_connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            active=True,
        )
        second_connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            member=UserFactory(),
            active=True,
        )
        for connection in (first_connection, second_connection):
            IssueAssigneeFactory(issue=issue, assignee=connection.member, project=issue.project)

        with patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            issue.name = "Paced update"
            issue.save(update_fields=["name", "updated_at"])

        assert {queued.args[2] for queued in enqueue.call_args_list} == {
            str(first_connection.id),
            str(second_connection.id),
        }
        assert all(queued.args[0].options["countdown"] == 0 for queued in enqueue.call_args_list)

    def test_nested_suppression_blocks_real_model_receivers(self):
        with patch("plane.db.signals.dispatch_google_calendar_issue_sync") as dispatch:
            issue = IssueFactory()
            dispatch.reset_mock()

            with suppress_google_calendar_issue_signal_dispatch():
                issue.name = "Suppressed update"
                issue.save(update_fields=["name", "updated_at"])
                with suppress_google_calendar_issue_signal_dispatch():
                    IssueAssigneeFactory(issue=issue)
                    IssueLabelFactory(issue=issue)

            dispatch.assert_not_called()

    def test_exception_restores_real_model_receiver_dispatch(self):
        with patch("plane.db.signals.dispatch_google_calendar_issue_sync") as dispatch:
            issue = IssueFactory()
            dispatch.reset_mock()

            with pytest.raises(RuntimeError, match="mutation failed"):
                with suppress_google_calendar_issue_signal_dispatch():
                    issue.name = "Failed update"
                    issue.save(update_fields=["name", "updated_at"])
                    raise RuntimeError("mutation failed")

            issue.name = "Recovered update"
            issue.save(update_fields=["name", "updated_at"])

            dispatch.assert_called_once_with(issue.id)

    def test_direct_issue_and_relation_saves_dispatch_outside_suppression(self):
        with patch("plane.db.signals.dispatch_google_calendar_issue_sync") as dispatch:
            issue = IssueFactory()
            assignee = IssueAssigneeFactory(issue=issue)
            label = IssueLabelFactory(issue=issue)
            dispatch.reset_mock()

            issue.name = "Direct issue update"
            issue.save(update_fields=["name", "updated_at"])
            assignee.save(update_fields=["updated_at"])
            label.save(update_fields=["updated_at"])

            assert dispatch.call_args_list == [
                call(issue.id),
                call(issue.id),
                call(issue.id),
            ]


@pytest.mark.unit
@pytest.mark.django_db
class TestGoogleCalendarStateSignals:
    def test_state_creation_does_not_enqueue_a_resync(self):
        with patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            StateFactory()

        enqueue.assert_not_called()

    def test_existing_state_save_enqueues_resync_after_commit(self):
        state = StateFactory()

        with patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            state.name = "Ready for review"
            state.save(update_fields=["name", "updated_at"])

        enqueue.assert_called_once()
        assert enqueue.call_args.args[1:] == (str(state.id),)

    def test_default_queryset_update_does_not_enqueue_a_resync(self):
        state = StateFactory()

        with patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            type(state).objects.filter(id=state.id).update(default=True)

        enqueue.assert_not_called()


@pytest.mark.unit
@pytest.mark.django_db
class TestGoogleCalendarLabelSignals:
    def test_label_creation_does_not_enqueue_a_resync(self):
        with patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            LabelFactory()

        enqueue.assert_not_called()

    def test_existing_label_save_enqueues_resync_after_commit(self):
        label = LabelFactory()

        with patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            label.name = "Customer request"
            label.save(update_fields=["name", "updated_at"])

        enqueue.assert_called_once()
        assert enqueue.call_args.args[1:] == (str(label.id),)


@pytest.mark.unit
@pytest.mark.django_db
class TestGoogleCalendarCycleSignals:
    def test_multi_cycle_dispatch_paces_each_connection_independently(self):
        workspace_integration = WorkspaceIntegrationFactory(
            integration__provider="google_calendar",
            config={"enabled": True, "recipients": "cycle_members"},
        )
        first_connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            active=True,
        )
        second_connection = GoogleCalendarConnectionFactory(
            workspace_integration=workspace_integration,
            member=UserFactory(),
            active=True,
        )
        cycles = [CycleFactory(project__workspace=workspace_integration.workspace) for _ in range(2)]
        for cycle in cycles:
            cycle_issue = CycleIssueFactory(cycle=cycle, project=cycle.project)
            for connection in (first_connection, second_connection):
                IssueAssigneeFactory(
                    issue=cycle_issue.issue,
                    assignee=connection.member,
                    project=cycle.project,
                )

        with patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            dispatch_google_calendar_cycle_syncs([cycle.id for cycle in cycles])

        countdowns_by_connection = {}
        for queued in enqueue.call_args_list:
            task, _, connection_id = queued.args
            countdowns_by_connection.setdefault(connection_id, []).append(task.options["countdown"])
        assert countdowns_by_connection == {
            str(first_connection.id): [0, 1],
            str(second_connection.id): [0, 1],
        }

    def test_cycle_creation_does_not_enqueue_convergence(self):
        with patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            CycleFactory()

        enqueue.assert_not_called()

    def test_existing_cycle_save_and_hard_delete_enqueue_convergence(self):
        cycle = CycleFactory()
        cycle_id = cycle.id

        with patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            cycle.name = "Release train"
            cycle.save(update_fields=["name", "updated_at"])
            cycle.delete(soft=False)

        assert [queued.args[1:] for queued in enqueue.call_args_list] == [
            (str(cycle_id),),
            (str(cycle_id),),
        ]

    def test_issue_dispatch_also_enqueues_its_current_cycle(self):
        cycle_issue = CycleIssueFactory()

        with patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            cycle_issue.issue.name = "Current cycle work item"
            cycle_issue.issue.save(update_fields=["name", "updated_at"])

        assert [queued.args[1:] for queued in enqueue.call_args_list] == [
            (str(cycle_issue.issue_id),),
            (str(cycle_issue.cycle_id),),
        ]
