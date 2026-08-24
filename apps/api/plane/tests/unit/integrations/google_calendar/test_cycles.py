# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

import pytest
from django.utils import timezone

from plane.db.models import CycleIssue, IssueAssignee
from plane.integrations.google_calendar.eligibility import (
    cycle_recipient_connection_ids,
    is_cycle_creatable,
    should_retain_cycle_event,
)
from plane.integrations.google_calendar.events import build_google_calendar_cycle_event
from plane.tests.factories import (
    CycleFactory,
    CycleIssueFactory,
    GoogleCalendarConnectionFactory,
    IssueAssigneeFactory,
    IssueFactory,
    ProjectMemberFactory,
    StateFactory,
    UserFactory,
    WorkspaceIntegrationFactory,
    WorkspaceMemberFactory,
)


@pytest.mark.unit
@pytest.mark.django_db
class TestGoogleCalendarCycleEvents:
    @pytest.mark.parametrize(
        ("cycle_timezone", "start", "end", "expected_start", "expected_end"),
        (
            (
                "America/Los_Angeles",
                datetime(2026, 8, 20, 7, tzinfo=datetime_timezone.utc),
                datetime(2026, 8, 24, 6, 59, tzinfo=datetime_timezone.utc),
                "2026-08-20",
                "2026-08-24",
            ),
            (
                "UTC",
                datetime(2026, 8, 20, 0, 0, 1, tzinfo=datetime_timezone.utc),
                datetime(2026, 8, 23, 23, 59, tzinfo=datetime_timezone.utc),
                "2026-08-20",
                "2026-08-24",
            ),
        ),
    )
    def test_payload_uses_the_persisted_timezone_snapshot(
        self,
        cycle_timezone,
        start,
        end,
        expected_start,
        expected_end,
    ):
        cycle = CycleFactory(
            name="Release train",
            project__name="Engineering",
            timezone=cycle_timezone,
            start_date=start,
            end_date=end,
        )

        payload = build_google_calendar_cycle_event(cycle)

        assert payload == {
            "summary": "Cycle: Release train — Engineering",
            "start": {"date": expected_start},
            "end": {"date": expected_end},
            "status": "confirmed",
            "reminders": {"useDefault": False},
            "extendedProperties": {
                "private": {
                    "plane_entity_type": "cycle",
                    "plane_entity_id": str(cycle.id),
                }
            },
        }
        assert "attendees" not in payload


@pytest.mark.unit
@pytest.mark.django_db
class TestGoogleCalendarCycleEligibility:
    def setup_method(self):
        self.workspace_integration = WorkspaceIntegrationFactory(
            integration__provider="google_calendar",
            config={"enabled": True, "recipients": "cycle_members"},
        )
        self.cycle = CycleFactory(project__workspace=self.workspace_integration.workspace)
        self.connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            active=True,
        )
        self.issue = IssueFactory(
            project=self.cycle.project,
            target_date=None,
            state=StateFactory(project=self.cycle.project, group="completed"),
        )
        CycleIssueFactory(cycle=self.cycle, issue=self.issue, project=self.cycle.project)
        IssueAssigneeFactory(issue=self.issue, assignee=self.connection.member, project=self.cycle.project)

    def test_default_cycle_members_ignore_dates_terminal_state_and_filters(self):
        self.workspace_integration.config.update(
            {
                "mode": "filter",
                "priorities": ["urgent"],
                "label_ids": ["b91e16c1-c2ef-4da9-8f62-506460a6f94d"],
            }
        )
        self.workspace_integration.save(update_fields=["config", "updated_at"])
        self.issue.priority = "low"

        assert cycle_recipient_connection_ids(self.cycle) == [self.connection.id]

    @pytest.mark.parametrize("excluded_kind", ("draft", "triage", "archive", "delete", "membership"))
    def test_default_cycle_members_exclude_non_syncable_items(self, excluded_kind):
        if excluded_kind == "draft":
            self.issue.is_draft = True
            self.issue.save(update_fields=["is_draft", "updated_at"])
        elif excluded_kind == "triage":
            self.issue.state.group = "triage"
            self.issue.state.save(update_fields=["group", "updated_at"])
        elif excluded_kind == "archive":
            self.issue.archived_at = timezone.now()
            self.issue.save(update_fields=["archived_at", "updated_at"])
        elif excluded_kind == "delete":
            self.issue.deleted_at = timezone.now()
            self.issue.save(update_fields=["deleted_at", "updated_at"])
        else:
            CycleIssue.objects.filter(cycle=self.cycle, issue=self.issue).delete()

        assert cycle_recipient_connection_ids(self.cycle) == []

    @pytest.mark.parametrize("recipient_mode", ("project_members", "workspace_members"))
    def test_broader_recipient_modes_resolve_only_healthy_members(self, recipient_mode):
        member = UserFactory()
        healthy_connection = GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            member=member,
            active=True,
        )
        unavailable_member = UserFactory()
        GoogleCalendarConnectionFactory(
            workspace_integration=self.workspace_integration,
            member=unavailable_member,
            desired_state="connected",
            status="error",
        )
        if recipient_mode == "project_members":
            ProjectMemberFactory(project=self.cycle.project, member=member)
            ProjectMemberFactory(project=self.cycle.project, member=unavailable_member)
        else:
            WorkspaceMemberFactory(workspace=self.cycle.workspace, member=member)
            WorkspaceMemberFactory(workspace=self.cycle.workspace, member=unavailable_member)
        self.workspace_integration.config["recipients"] = recipient_mode
        self.workspace_integration.save(update_fields=["config", "updated_at"])

        assert cycle_recipient_connection_ids(self.cycle) == [healthy_connection.id]

    def test_ended_cycles_are_retained_but_not_creatable_for_new_recipients(self):
        self.cycle.end_date = timezone.now() - timedelta(days=2)
        self.cycle.start_date = self.cycle.end_date - timedelta(days=7)

        assert is_cycle_creatable(self.cycle, self.connection) is False
        assert should_retain_cycle_event(self.cycle, self.connection) is True

        IssueAssignee.objects.filter(issue=self.issue, assignee=self.connection.member).delete()

        assert should_retain_cycle_event(self.cycle, self.connection) is False

    @pytest.mark.parametrize("exclusion", ("project_opt_out", "project_archive", "cycle_archive"))
    def test_project_and_cycle_exclusions_apply_before_recipient_resolution(self, exclusion):
        if exclusion == "project_opt_out":
            self.cycle.project.google_calendar_sync_enabled = False
        elif exclusion == "project_archive":
            self.cycle.project.archived_at = timezone.now()
        else:
            self.cycle.archived_at = timezone.now()

        with patch("plane.integrations.google_calendar.eligibility.cycle_recipient_connection_ids") as recipients:
            assert is_cycle_creatable(self.cycle, self.connection) is False

        recipients.assert_not_called()
