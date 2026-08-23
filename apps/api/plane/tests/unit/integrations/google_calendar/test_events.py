# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import date

import pytest
from django.test import override_settings

from plane.db.models import IssueLabel
from plane.integrations.google_calendar.events import build_google_calendar_work_item_event
from plane.tests.factories import IssueFactory, IssueLabelFactory, LabelFactory


@pytest.mark.unit
@pytest.mark.django_db
class TestGoogleCalendarWorkItemEvents:
    @override_settings(APP_BASE_URL="https://plane.example", WEB_URL="https://api.plane.example")
    def test_normal_payload_is_canonical_and_all_day(self):
        issue = IssueFactory(
            project__identifier="ENG",
            project__name="Engineering",
            project__workspace__slug="acme",
            name="Ship calendar sync",
            priority="high",
            start_date=date(2026, 8, 20),
            target_date=date(2026, 8, 23),
            state__name="In Progress",
            state__group="started",
        )
        issue.sequence_id = 42
        IssueLabelFactory(issue=issue, label=LabelFactory(project=issue.project, name="Zulu"), project=issue.project)
        IssueLabelFactory(issue=issue, label=LabelFactory(project=issue.project, name="alpha"), project=issue.project)

        payload = build_google_calendar_work_item_event(issue)

        assert payload == {
            "summary": "[ENG-42] Ship calendar sync",
            "description": "\n".join(
                (
                    "Plane: https://plane.example/acme/browse/ENG-42",
                    "Project: Engineering",
                    "State: In Progress",
                    "Priority: high",
                    "Labels: alpha, Zulu",
                )
            ),
            "start": {"date": "2026-08-20"},
            "end": {"date": "2026-08-24"},
            "status": "confirmed",
            "reminders": {"useDefault": False},
            "extendedProperties": {
                "private": {
                    "plane_entity_type": "work_item",
                    "plane_entity_id": str(issue.id),
                }
            },
        }
        assert "attendees" not in payload

    @override_settings(APP_BASE_URL="https://plane.example")
    def test_description_excludes_soft_deleted_issue_labels(self):
        issue = IssueFactory()
        IssueLabelFactory(
            issue=issue,
            label=LabelFactory(project=issue.project, name="Active"),
            project=issue.project,
        )
        removed_label = IssueLabelFactory(
            issue=issue,
            label=LabelFactory(project=issue.project, name="Removed"),
            project=issue.project,
        )
        IssueLabel.objects.filter(id=removed_label.id).delete()

        payload = build_google_calendar_work_item_event(issue)

        assert payload["description"].endswith("Labels: Active")
        assert "Removed" not in payload["description"]

    @override_settings(APP_BASE_URL="https://plane.example")
    @pytest.mark.parametrize(
        ("state_group", "prefix"),
        (("completed", "[Completed]"), ("cancelled", "[Cancelled]")),
    )
    def test_terminal_payload_uses_fixed_prefix_and_green_color(self, state_group, prefix):
        issue = IssueFactory(
            project__identifier="CAL",
            state__group=state_group,
            start_date=date(2026, 8, 24),
            target_date=date(2026, 8, 23),
        )

        payload = build_google_calendar_work_item_event(issue)

        assert payload["summary"].startswith(f"{prefix} [CAL-")
        assert payload["colorId"] == "10"
        assert payload["start"] == {"date": "2026-08-23"}
        assert payload["end"] == {"date": "2026-08-24"}
