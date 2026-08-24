# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from plane.db.models import GoogleCalendarConnection, IssueAssignee, IssueLabel, Label
from plane.integrations.google_calendar.eligibility import (
    is_issue_assignment_eligible,
    is_issue_open_backfill_eligible,
)
from plane.tests.factories import (
    GoogleCalendarConnectionFactory,
    IssueAssigneeFactory,
    IssueFactory,
    IssueLabelFactory,
    LabelFactory,
    StateFactory,
    UserFactory,
    WorkspaceFactory,
    WorkspaceIntegrationFactory,
)


@pytest.mark.unit
@pytest.mark.django_db
class TestGoogleCalendarIssueEligibility:
    def setup_method(self):
        self.workspace_integration = WorkspaceIntegrationFactory(
            config={"enabled": True, "mode": "assignment", "update_on_completion": True}
        )
        self.issue = IssueFactory(project__workspace=self.workspace_integration.workspace)
        self.connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            active=True,
        )
        IssueAssigneeFactory(
            issue=self.issue,
            assignee=self.connection.member,
            project=self.issue.project,
        )

    def test_due_assigned_item_on_a_healthy_connection_is_eligible(self):
        assert is_issue_assignment_eligible(self.issue, self.connection) is True

    @pytest.mark.parametrize(
        ("mutation", "value"),
        (
            ("policy", False),
            ("due_date", None),
            ("draft", True),
            ("archive", timezone.localdate()),
            ("delete", timezone.now()),
            ("triage", "triage"),
            ("connection_status", GoogleCalendarConnection.Status.ERROR),
            ("calendar_id", ""),
        ),
    )
    def test_base_rule_loss_makes_an_item_ineligible(self, mutation, value):
        if mutation == "policy":
            self.workspace_integration.config = {**self.workspace_integration.config, "enabled": value}
        elif mutation == "due_date":
            self.issue.target_date = value
        elif mutation == "draft":
            self.issue.is_draft = value
        elif mutation == "archive":
            self.issue.archived_at = value
        elif mutation == "delete":
            self.issue.deleted_at = value
        elif mutation == "triage":
            self.issue.state.group = value
        elif mutation == "connection_status":
            self.connection.status = value
        elif mutation == "calendar_id":
            self.connection.calendar_id = value

        assert is_issue_assignment_eligible(self.issue, self.connection) is False

    def test_foreign_workspace_and_unassigned_receivers_are_ineligible(self):
        foreign_connection = GoogleCalendarConnectionFactory(
            workspace_integration=WorkspaceIntegrationFactory(
                workspace=WorkspaceFactory(),
                config={"enabled": True, "mode": "assignment"},
            ),
            active=True,
        )
        unassigned_connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            member=UserFactory(),
            active=True,
        )

        assert is_issue_assignment_eligible(self.issue, foreign_connection) is False
        assert is_issue_assignment_eligible(self.issue, unassigned_connection) is False

    @pytest.mark.parametrize("state_group", ("completed", "cancelled"))
    def test_open_backfill_excludes_terminal_items(self, state_group):
        self.issue.state = StateFactory(project=self.issue.project, group=state_group)

        assert is_issue_assignment_eligible(self.issue, self.connection) is True
        assert is_issue_open_backfill_eligible(self.issue, self.connection) is False

    def test_open_backfill_keeps_overdue_open_items(self):
        self.issue.target_date = timezone.localdate() - timedelta(days=30)

        assert is_issue_open_backfill_eligible(self.issue, self.connection) is True

    def test_project_inclusion_extension_can_add_a_project_exclusion(self):
        assert (
            is_issue_assignment_eligible(
                self.issue,
                self.connection,
                project_inclusion=lambda issue, connection: False,
            )
            is False
        )

    @pytest.mark.parametrize(
        ("priorities", "issue_priority", "expected"),
        (
            ([], "low", True),
            (["urgent", "high"], "urgent", True),
            (["urgent", "high"], "high", True),
            (["urgent", "high"], "medium", False),
        ),
    )
    def test_filter_mode_priorities_use_or(self, priorities, issue_priority, expected):
        self.workspace_integration.config = {
            "enabled": True,
            "mode": "filter",
            "priorities": priorities,
            "label_ids": [],
        }
        self.issue.priority = issue_priority

        assert is_issue_assignment_eligible(self.issue, self.connection) is expected

    @pytest.mark.parametrize(
        ("label_match", "selected_label_indexes", "expected"),
        (
            ("any", (), True),
            ("any", (0, 2), True),
            ("any", (2,), False),
            ("all", (0, 1), True),
            ("all", (0, 2), False),
        ),
    )
    def test_filter_mode_labels_follow_any_all_and_empty_rules(
        self,
        label_match,
        selected_label_indexes,
        expected,
    ):
        labels = [LabelFactory(project=self.issue.project) for _ in range(3)]
        for label in labels[:2]:
            IssueLabelFactory(issue=self.issue, label=label, project=self.issue.project)
        self.workspace_integration.config = {
            "enabled": True,
            "mode": "filter",
            "priorities": [],
            "label_ids": [str(labels[index].id) for index in selected_label_indexes],
            "label_match": label_match,
        }

        assert is_issue_assignment_eligible(self.issue, self.connection) is expected

    @pytest.mark.parametrize(
        ("issue_priority", "has_selected_label", "expected"),
        (
            ("urgent", True, True),
            ("high", True, True),
            ("medium", True, False),
            ("urgent", False, False),
        ),
    )
    def test_populated_filter_categories_are_anded(self, issue_priority, has_selected_label, expected):
        selected_label = LabelFactory(project=self.issue.project, name="client-work")
        if has_selected_label:
            IssueLabelFactory(issue=self.issue, label=selected_label, project=self.issue.project)
        self.workspace_integration.config = {
            "enabled": True,
            "mode": "filter",
            "priorities": ["urgent", "high"],
            "label_ids": [str(selected_label.id)],
            "label_match": "any",
        }
        self.issue.priority = issue_priority

        assert is_issue_assignment_eligible(self.issue, self.connection) is expected

    @pytest.mark.parametrize("stale_kind", ("missing_label", "deleted_label", "deleted_relation"))
    def test_stale_or_deleted_labels_do_not_satisfy_filter_mode(self, stale_kind):
        selected_label = LabelFactory(project=self.issue.project)
        relation = IssueLabelFactory(issue=self.issue, label=selected_label, project=self.issue.project)
        selected_label_id = selected_label.id
        if stale_kind == "missing_label":
            selected_label_id = uuid4()
        elif stale_kind == "deleted_label":
            Label.all_objects.filter(id=selected_label.id).update(deleted_at=timezone.now())
        else:
            IssueLabel.all_objects.filter(id=relation.id).update(deleted_at=timezone.now())
        self.workspace_integration.config = {
            "enabled": True,
            "mode": "filter",
            "priorities": [],
            "label_ids": [str(selected_label_id)],
            "label_match": "any",
        }

        assert is_issue_assignment_eligible(self.issue, self.connection) is False

    def test_filter_mode_supports_the_current_single_value_policy(self):
        selected_label = LabelFactory(project=self.issue.project)
        IssueLabelFactory(issue=self.issue, label=selected_label, project=self.issue.project)
        self.workspace_integration.config = {
            "enabled": True,
            "mode": "filter",
            "priority": "urgent",
            "label_id": str(selected_label.id),
        }
        self.issue.priority = "urgent"
        IssueAssignee.objects.filter(issue_id=self.issue.id, assignee_id=self.connection.member_id).delete()

        assert is_issue_assignment_eligible(self.issue, self.connection) is True

    def test_assignment_mode_ignores_filter_categories_and_still_requires_assignment(self):
        selected_label = LabelFactory(project=self.issue.project)
        self.workspace_integration.config = {
            "enabled": True,
            "mode": "assignment",
            "priorities": ["urgent"],
            "label_ids": [str(selected_label.id)],
            "label_match": "all",
        }
        self.issue.priority = "low"

        assert is_issue_assignment_eligible(self.issue, self.connection) is True

        IssueAssignee.objects.filter(issue_id=self.issue.id, assignee_id=self.connection.member_id).delete()

        assert is_issue_assignment_eligible(self.issue, self.connection) is False
