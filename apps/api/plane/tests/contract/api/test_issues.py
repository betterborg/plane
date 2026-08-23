# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest import mock

import pytest
from rest_framework import status

from plane.app.serializers.issue import IssueCreateSerializer
from plane.db.models import Issue, IssueAssignee, IssueLabel, Label, Project, ProjectMember, State
from plane.db.signals import suppress_google_calendar_issue_signal_dispatch


@pytest.fixture
def project(db, workspace, create_user):
    """Create a test project with the requesting user as an active member."""
    project = Project.objects.create(
        name="Test Project",
        identifier="TP",
        workspace=workspace,
        created_by=create_user,
    )
    ProjectMember.objects.create(
        project=project,
        member=create_user,
        role=20,  # Admin
        is_active=True,
    )
    return project


@pytest.fixture
def state(db, workspace, project):
    return State.objects.create(
        name="Todo",
        project=project,
        workspace=workspace,
        group="backlog",
        default=True,
    )


@pytest.fixture
def issue(db, workspace, project, state, create_user):
    return Issue.objects.create(
        name="Test Issue",
        workspace=workspace,
        project=project,
        state=state,
        created_by=create_user,
    )


@pytest.fixture
def label(db, workspace, project):
    return Label.objects.create(name="Calendar", workspace=workspace, project=project, color="#60646C")


def assert_calendar_dispatch_sees_relations(enqueue, assignee_ids, label_ids):
    assert enqueue.call_count == 1
    issue_id = enqueue.call_args.args[1]
    assert set(IssueAssignee.objects.filter(issue_id=issue_id).values_list("assignee_id", flat=True)) == set(
        assignee_ids
    )
    assert set(IssueLabel.objects.filter(issue_id=issue_id).values_list("label_id", flat=True)) == set(label_ids)


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
class TestIssueCalendarDispatch:
    def public_collection_url(self, workspace_slug, project_id):
        return f"/api/v1/workspaces/{workspace_slug}/projects/{project_id}/work-items/"

    def test_app_create_and_update_each_dispatch_once_after_relations(
        self, workspace, project, state, label, create_user
    ):
        create_payload = {
            "name": "App-created work item",
            "state_id": str(state.id),
            "target_date": "2026-09-01",
            "assignee_ids": [str(create_user.id)],
            "label_ids": [str(label.id)],
        }
        context = {
            "project_id": project.id,
            "workspace_id": workspace.id,
            "default_assignee_id": None,
        }
        serializer = IssueCreateSerializer(data=create_payload, context=context)
        assert serializer.is_valid(), serializer.errors

        with mock.patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            created_issue = serializer.save()

        assert_calendar_dispatch_sees_relations(enqueue, [create_user.id], [label.id])

        update_serializer = IssueCreateSerializer(
            created_issue,
            data={"name": "App-updated work item", "assignee_ids": [], "label_ids": []},
            partial=True,
            context={"project_id": project.id},
        )
        assert update_serializer.is_valid(), update_serializer.errors

        with mock.patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            update_serializer.save()

        assert_calendar_dispatch_sees_relations(enqueue, [], [])

    def test_public_post_dispatches_once_and_suppresses_audit_save(
        self, api_key_client, workspace, project, state, label, create_user
    ):
        payload = {
            "name": "Public POST work item",
            "state": str(state.id),
            "target_date": "2026-09-01",
            "assignees": [str(create_user.id)],
            "labels": [str(label.id)],
        }

        with mock.patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            response = api_key_client.post(
                self.public_collection_url(workspace.slug, project.id),
                payload,
                format="json",
            )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert_calendar_dispatch_sees_relations(enqueue, [create_user.id], [label.id])

    def test_public_create_on_upsert_dispatches_once_and_suppresses_audit_save(
        self, api_key_client, workspace, project, state, label, create_user
    ):
        payload = {
            "name": "Public PUT-created work item",
            "state": str(state.id),
            "target_date": "2026-09-01",
            "external_id": "calendar-create",
            "external_source": "calendar-test",
            "assignees": [str(create_user.id)],
            "labels": [str(label.id)],
        }

        with mock.patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            response = api_key_client.put(
                self.public_collection_url(workspace.slug, project.id),
                payload,
                format="json",
            )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert_calendar_dispatch_sees_relations(enqueue, [create_user.id], [label.id])

    def test_public_existing_upsert_dispatches_once_after_updated_relations(
        self, api_key_client, workspace, project, state, label, create_user
    ):
        with suppress_google_calendar_issue_signal_dispatch():
            existing_issue = Issue.objects.create(
                name="Existing public work item",
                project=project,
                state=state,
                target_date="2026-09-01",
                external_id="calendar-update",
                external_source="calendar-test",
            )

        payload = {
            "name": "Updated public work item",
            "external_id": existing_issue.external_id,
            "external_source": existing_issue.external_source,
            "assignees": [str(create_user.id)],
            "labels": [str(label.id)],
        }

        with mock.patch("plane.db.signals.enqueue_google_calendar_task_on_commit") as enqueue:
            response = api_key_client.put(
                self.public_collection_url(workspace.slug, project.id),
                payload,
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert_calendar_dispatch_sees_relations(enqueue, [create_user.id], [label.id])


@pytest.mark.contract
class TestIssueListOrderByInjection:
    """Regression tests for GHSA-p885-6jpg-cr2p on the work-item list
    endpoint: GET /api/v1/workspaces/{slug}/projects/{project_id}/issues/.

    The raw ``order_by`` query parameter fell through the endpoint's hardcoded
    branch logic to ``issue_queryset.order_by(order_by_param)``, letting an
    attacker order by sensitive related columns (blind oracle) or crash the
    endpoint with an unknown field (HTTP 500). The fix sanitizes the parameter
    against ISSUE_ORDER_BY_ALLOWLIST before the branch logic runs.
    """

    def get_url(self, workspace_slug, project_id):
        return f"/api/v1/workspaces/{workspace_slug}/projects/{project_id}/issues/"

    @pytest.mark.django_db
    def test_invalid_order_by_does_not_500(self, api_key_client, workspace, project, issue):
        """Unknown field used to raise FieldError → HTTP 500; now sanitized to
        the safe default and returns 200 (DoS half of the advisory)."""
        url = self.get_url(workspace.slug, project.id)
        response = api_key_client.get(url, {"order_by": "not_a_field"})

        assert response.status_code == status.HTTP_200_OK, f"Got {response.status_code}: {response.data!r}"

    @pytest.mark.django_db
    def test_relational_order_by_injection_does_not_500(self, api_key_client, workspace, project, issue):
        """Ordering by a related-table column (``created_by__password``) used to
        reach ``.order_by()`` raw, forming a blind ordering oracle. It is now
        neutralized to the safe default. (Deterministic neutralization is
        asserted in tests/unit/utils/test_order_by_sanitize.py.)"""
        url = self.get_url(workspace.slug, project.id)
        response = api_key_client.get(url, {"order_by": "created_by__password"})

        assert response.status_code == status.HTTP_200_OK, f"Got {response.status_code}: {response.data!r}"

    @pytest.mark.django_db
    def test_legitimate_order_by_still_works(self, api_key_client, workspace, project, issue):
        """A valid, allowlisted ordering value continues to return 200 —
        the sanitizer must not break legitimate ordering."""
        url = self.get_url(workspace.slug, project.id)

        for value in ["-created_at", "priority", "state__group", "sequence_id"]:
            response = api_key_client.get(url, {"order_by": value})
            assert response.status_code == status.HTTP_200_OK, (
                f"order_by={value!r} got {response.status_code}: {response.data!r}"
            )
