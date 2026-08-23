# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta

import pytest
from django.utils import timezone

from plane.db.models import GoogleCalendarConnection
from plane.integrations.google_calendar.eligibility import (
    is_issue_assignment_eligible,
    is_issue_open_backfill_eligible,
)
from plane.tests.factories import (
    GoogleCalendarConnectionFactory,
    IssueAssigneeFactory,
    IssueFactory,
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
