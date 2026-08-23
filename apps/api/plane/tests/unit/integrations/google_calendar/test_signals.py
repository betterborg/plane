# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import call, patch

import pytest

from plane.db.signals import suppress_google_calendar_issue_signal_dispatch
from plane.tests.factories import IssueAssigneeFactory, IssueFactory, IssueLabelFactory


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
class TestGoogleCalendarIssueSignals:
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
