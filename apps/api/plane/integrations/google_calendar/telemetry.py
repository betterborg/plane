# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Secret-free operational telemetry for the Google Calendar integration."""

from uuid import UUID

from plane.utils.analytics_events import (
    GOOGLE_CALENDAR_ADOPTED,
    GOOGLE_CALENDAR_CREDENTIAL_MISMATCH,
    GOOGLE_CALENDAR_INVENTORY_RESET,
    GOOGLE_CALENDAR_LIFECYCLE_RECOVERY,
    GOOGLE_CALENDAR_PUBLICATION_FAILURE,
    GOOGLE_CALENDAR_RECONCILIATION_OVERDUE,
)


_ANALYTICS_EVENTS = frozenset(
    {
        GOOGLE_CALENDAR_ADOPTED,
        GOOGLE_CALENDAR_CREDENTIAL_MISMATCH,
        GOOGLE_CALENDAR_INVENTORY_RESET,
        GOOGLE_CALENDAR_LIFECYCLE_RECOVERY,
        GOOGLE_CALENDAR_PUBLICATION_FAILURE,
        GOOGLE_CALENDAR_RECONCILIATION_OVERDUE,
    }
)
_OPERATIONS = frozenset(
    {
        "credential_binding",
        "cycle_publication",
        "health_notice_publication",
        "lifecycle_reconciliation",
        "lifecycle_recovery",
        "local_inventory",
        "provider_inventory",
        "rejected_grant_revocation",
        "release_readiness",
        "scheduled_reconciliation",
        "task_publication",
        "work_item_publication",
        "workspace_adoption",
    }
)
_OPERATIONAL_FIELDS = frozenset(
    {
        "workspace_id",
        "connection_id",
        "entity_id",
        "outcome",
        "attempt",
        "enqueue_latency_ms",
        "google_status_class",
        "inventory_mode",
        "provider_page_count",
        "local_candidate_count",
        "calendar_generation",
        "reconciliation_action",
        "last_completion_age_seconds",
        "publication_failure_class",
    }
)


def _safe_operational_fields(fields):
    """Return only the explicitly approved, scalar operational fields."""

    safe_fields = {}
    for key, value in fields.items():
        if key not in _OPERATIONAL_FIELDS or value is None:
            continue
        if key.endswith("_id") and isinstance(value, (str, int, UUID)):
            safe_fields[key] = str(value)
        elif isinstance(value, (str, int, float, bool)):
            safe_fields[key] = value
    return safe_fields


def log_google_calendar_operation(logger, operation, **fields):
    """Write one structured Calendar log without accepting secret-bearing fields."""

    safe_operation = operation if isinstance(operation, str) and operation in _OPERATIONS else "unrecognized"
    log_method = logger.error if fields.get("outcome") in {"failed", "publication_failed", "mismatch"} else logger.info
    log_method(
        "Google Calendar operation",
        extra={
            "operation": safe_operation,
            **_safe_operational_fields(fields),
        },
    )


def publish_google_calendar_analytics(event_name, *, user_id, workspace_id, workspace_slug, **fields):
    """Publish an approved Calendar analytics event without leaking provider data."""

    if not isinstance(event_name, str) or event_name not in _ANALYTICS_EVENTS:
        return False

    from plane.bgtasks.event_tracking_task import track_event

    try:
        track_event.delay(
            user_id=user_id,
            event_name=event_name,
            slug=workspace_slug,
            event_properties={
                "workspace_id": str(workspace_id),
                **_safe_operational_fields(fields),
            },
        )
    except Exception:
        # Analytics is observational and must never alter Calendar convergence.
        return False
    return True
