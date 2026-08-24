# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Secret-free operational telemetry for the Google Calendar integration."""

from uuid import UUID

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

    log_method = logger.error if fields.get("outcome") in {"failed", "publication_failed", "mismatch"} else logger.info
    log_method(
        "Google Calendar operation",
        extra={"operation": operation, **_safe_operational_fields(fields)},
    )


def publish_google_calendar_analytics(event_name, *, user_id, workspace_id, workspace_slug, **fields):
    """Publish an approved Calendar analytics event without leaking provider data."""

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
