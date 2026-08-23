# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import hashlib
import json
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from plane.db.models import GoogleCalendarEvent, IssueLabel
from plane.db.models.state import StateGroup


GOOGLE_CALENDAR_TERMINAL_COLOR_ID = "10"


def google_calendar_event_id(connection_id, entity_type, entity_id):
    """Build a provider-valid deterministic ID scoped to one connection."""

    identity = f"{connection_id}:{entity_type}:{entity_id}".encode()
    return f"plane{hashlib.sha256(identity).hexdigest()}"


def google_calendar_payload_hash(payload):
    """Hash the canonical JSON representation used for provider convergence."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def canonical_issue_browse_url(issue):
    """Return the stable human-facing Plane work-item URL."""

    base_url = settings.APP_BASE_URL or settings.WEB_URL
    if not base_url:
        raise ImproperlyConfigured("APP_BASE_URL or WEB_URL is required for Calendar event links")
    issue_key = f"{issue.project.identifier}-{issue.sequence_id}"
    return f"{base_url.rstrip('/')}/{issue.workspace.slug}/browse/{issue_key}"


def _normal_summary(issue):
    return f"[{issue.project.identifier}-{issue.sequence_id}] {issue.name}"


def _description(issue):
    label_names = sorted(
        IssueLabel.objects.filter(issue_id=issue.id, deleted_at__isnull=True).values_list("label__name", flat=True),
        key=str.casefold,
    )
    return "\n".join(
        (
            f"Plane: {canonical_issue_browse_url(issue)}",
            f"Project: {issue.project.name}",
            f"State: {issue.state.name}",
            f"Priority: {issue.priority}",
            f"Labels: {', '.join(label_names)}",
        )
    )


def build_google_calendar_work_item_event(issue):
    """Construct the complete deterministic Google all-day event payload."""

    due_date = issue.target_date
    start_date = issue.start_date if issue.start_date and issue.start_date <= due_date else due_date
    summary = _normal_summary(issue)
    terminal = issue.state.group in {StateGroup.COMPLETED, StateGroup.CANCELLED}
    if issue.state.group == StateGroup.COMPLETED:
        summary = f"[Completed] {summary}"
    elif issue.state.group == StateGroup.CANCELLED:
        summary = f"[Cancelled] {summary}"

    payload = {
        "summary": summary,
        "description": _description(issue),
        "start": {"date": start_date.isoformat()},
        "end": {"date": (due_date + timedelta(days=1)).isoformat()},
        "status": "confirmed",
        "reminders": {"useDefault": False},
        "extendedProperties": {
            "private": {
                "plane_entity_type": GoogleCalendarEvent.EntityType.WORK_ITEM,
                "plane_entity_id": str(issue.id),
            }
        },
    }
    if terminal:
        payload["colorId"] = GOOGLE_CALENDAR_TERMINAL_COLOR_ID
    return payload
