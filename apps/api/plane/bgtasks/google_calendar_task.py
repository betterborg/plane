# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
from datetime import timedelta
from uuid import UUID, uuid4

from celery import current_app, shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from plane.db.models import (
    Cycle,
    GoogleCalendarConnection,
    GoogleCalendarEvent,
    Issue,
    IssueAssignee,
    IssueLabel,
    Project,
    WorkspaceIntegration,
)
from plane.db.models.state import StateGroup
from plane.integrations.google_calendar.client import (
    GoogleCalendarCalendarAbsent,
    GoogleCalendarClient,
    GoogleCalendarClientConflict,
    GoogleCalendarClientError,
    GoogleCalendarCredentialMismatch,
    GoogleCalendarEventAbsent,
    GoogleCalendarSyncTokenExpired,
)
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_CYCLE_BACKFILL_TASK,
    GOOGLE_CALENDAR_CYCLE_SYNC_TASK,
    GOOGLE_CALENDAR_ISSUE_SYNC_TASK,
    GOOGLE_CALENDAR_INVENTORY_TASK,
    GOOGLE_CALENDAR_LABEL_ISSUE_RESYNC_TASK,
    GOOGLE_CALENDAR_LIFECYCLE_TASK,
    GOOGLE_CALENDAR_OPEN_BACKFILL_TASK,
    GOOGLE_CALENDAR_PROJECT_CYCLE_RESYNC_TASK,
    GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK,
    GOOGLE_CALENDAR_STATE_ISSUE_RESYNC_TASK,
    GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_METADATA_KEY,
    GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_TASK,
    GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY,
    GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_RECONCILIATION_TASK,
    GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK,
    enqueue_google_calendar_task_on_commit,
    publish_google_calendar_task,
)
from plane.integrations.google_calendar.eligibility import (
    cycle_recipient_connection_ids,
    is_cycle_creatable,
    is_cycle_sync_enabled,
    is_issue_assignment_eligible,
    is_issue_open_backfill_eligible,
    should_retain_cycle_event,
)
from plane.integrations.google_calendar.events import (
    build_google_calendar_cycle_event,
    build_google_calendar_work_item_event,
    google_calendar_event_id,
    google_calendar_payload_hash,
    google_calendar_provider_payload_hash,
)
from plane.integrations.google_calendar.lifecycle import (
    complete_google_calendar_disconnect,
    has_usable_google_calendar_grant,
    lock_google_calendar_connection,
    mark_google_calendar_connection_active,
    mark_google_calendar_connection_error,
    record_google_calendar_cleanup_error,
)
from plane.integrations.google_calendar.oauth import GoogleCalendarOAuthConfigurationError


GOOGLE_CALENDAR_RECONCILIATION_LEASE = timedelta(minutes=5)
GOOGLE_CALENDAR_RECONCILIATION_PROVIDER_PAGE_LIMIT = 5
GOOGLE_CALENDAR_RECONCILIATION_LOCAL_PAGE_SIZE = 1000
GOOGLE_CALENDAR_RECONCILIATION_PROVIDER_PHASE = "provider_inventory"
GOOGLE_CALENDAR_RECONCILIATION_LOCAL_PHASE = "local_scan"


def _owns_generation(connection, generation, *, desired_state, status):
    return (
        connection.lifecycle_generation == generation
        and connection.desired_state == desired_state
        and connection.status == status
    )


def _client_for(connection):
    return GoogleCalendarClient(
        credential_fingerprint=connection.credential_fingerprint,
        access_token=connection.access_token,
        refresh_token=connection.refresh_token,
        token_expires_at=connection.token_expires_at,
        persist_access_token=lambda access_token: _persist_access_token(connection, access_token),
    )


def _persist_access_token(connection, access_token):
    connection.access_token = access_token.value
    connection.token_expires_at = access_token.expires_at
    connection.save(update_fields=["access_token", "token_expires_at", "updated_at"])


def _persist_refreshed_access_token(connection, client):
    access_token = client.access_token
    if access_token is None:
        return
    if connection.access_token == access_token.value and connection.token_expires_at == access_token.expires_at:
        return
    connection.access_token = access_token.value
    connection.token_expires_at = access_token.expires_at
    connection.save(update_fields=["access_token", "token_expires_at", "updated_at"])


def _mark_credential_mismatch(connection):
    update_fields = ["last_error", "updated_at"]
    connection.last_error = GoogleCalendarCredentialMismatch.classification
    if connection.desired_state == GoogleCalendarConnection.DesiredState.CONNECTED:
        connection.status = GoogleCalendarConnection.Status.ERROR
        update_fields.append("status")
    connection.save(update_fields=update_fields)


class _GoogleCalendarProviderRetry:
    """Carry a provider failure through a transaction so refreshed tokens can commit."""

    def __init__(self, error):
        self.error = error


def _issue_queryset():
    return Issue.all_objects.select_related("workspace", "project", "state")


def _cycle_queryset():
    return Cycle.all_objects.select_related("workspace", "project")


def _client_event_id_from_recovery(client, connection, entity_id, deterministic_event_id):
    existing_event = client.get_event(connection.calendar_id, deterministic_event_id)
    if existing_event is not None:
        return deterministic_event_id

    marker = f"plane_entity_id={entity_id}"
    recovered_events = client.list_events(
        connection.calendar_id,
        private_extended_property=marker,
    )
    for event in recovered_events:
        recovered_id = event.get("id")
        if isinstance(recovered_id, str) and recovered_id:
            return recovered_id
    return None


def _delete_provider_event(connection, correlation):
    if connection.calendar_id and correlation.calendar_generation == connection.calendar_generation:
        client = _client_for(connection)
        try:
            client.delete_event(connection.calendar_id, correlation.google_event_id)
        except GoogleCalendarCredentialMismatch:
            _mark_credential_mismatch(connection)
            return False
        except GoogleCalendarClientError as exc:
            _persist_refreshed_access_token(connection, client)
            return _GoogleCalendarProviderRetry(exc)
        _persist_refreshed_access_token(connection, client)
    correlation.delete(soft=False)
    return True


def _converge_provider_event(connection, entity_type, entity_id, payload, correlation):
    payload_hash = google_calendar_payload_hash(payload)
    if (
        correlation is not None
        and correlation.calendar_generation == connection.calendar_generation
        and correlation.payload_hash == payload_hash
        and correlation.provider_payload_hash == payload_hash
        and correlation.provider_status == "confirmed"
    ):
        return "unchanged"

    client = _client_for(connection)
    try:
        return _converge_provider_event_with_client(
            client,
            connection,
            entity_type,
            entity_id,
            payload,
            payload_hash,
            correlation,
        )
    except GoogleCalendarEventAbsent:
        return _request_event_calendar_replacement(connection)
    except GoogleCalendarCredentialMismatch:
        _mark_credential_mismatch(connection)
        return "credential_mismatch"
    except GoogleCalendarClientError as exc:
        _persist_refreshed_access_token(connection, client)
        return _GoogleCalendarProviderRetry(exc)


def _request_event_calendar_replacement(connection):
    """Move an active connection back through lifecycle validation after collection absence."""

    if (
        connection.desired_state != GoogleCalendarConnection.DesiredState.CONNECTED
        or connection.status != GoogleCalendarConnection.Status.ACTIVE
    ):
        return "stale"
    connection.status = GoogleCalendarConnection.Status.PENDING
    connection.reconciliation_phase = ""
    connection.reconciliation_cursor = ""
    connection.reconciliation_lease_expires_at = None
    connection.save(
        update_fields=[
            "status",
            "reconciliation_phase",
            "reconciliation_cursor",
            "reconciliation_lease_expires_at",
            "updated_at",
        ]
    )
    lifecycle_task = current_app.signature(GOOGLE_CALENDAR_LIFECYCLE_TASK)
    enqueue_google_calendar_task_on_commit(
        lifecycle_task,
        str(connection.id),
        connection.lifecycle_generation,
    )
    return "replacement_pending"


def _converge_provider_event_with_client(
    client,
    connection,
    entity_type,
    entity_id,
    payload,
    payload_hash,
    correlation,
):
    deterministic_event_id = google_calendar_event_id(connection.id, entity_type, entity_id)
    current_generation_correlation = (
        correlation is not None and correlation.calendar_generation == connection.calendar_generation
    )
    if not current_generation_correlation:
        provider_event_id = deterministic_event_id
        try:
            provider_event = client.insert_event(connection.calendar_id, provider_event_id, payload)
        except GoogleCalendarClientConflict:
            provider_event_id = _client_event_id_from_recovery(
                client,
                connection,
                entity_id,
                deterministic_event_id,
            )
            if provider_event_id is None:
                provider_event = client.insert_event(connection.calendar_id, deterministic_event_id, payload)
                provider_event_id = deterministic_event_id
            else:
                provider_event = _update_or_recreate_provider_event(
                    client,
                    connection.calendar_id,
                    deterministic_event_id,
                    provider_event_id,
                    payload,
                )
                provider_event_id = _provider_event_id(provider_event, provider_event_id)
        observation = _provider_observation(provider_event, payload_hash)
        if correlation is None:
            GoogleCalendarEvent.objects.create(
                connection=connection,
                entity_type=entity_type,
                entity_id=entity_id,
                google_event_id=provider_event_id,
                payload_hash=payload_hash,
                calendar_generation=connection.calendar_generation,
                last_synced_at=timezone.now(),
                **observation,
            )
        else:
            correlation.google_event_id = provider_event_id
            correlation.payload_hash = payload_hash
            correlation.calendar_generation = connection.calendar_generation
            correlation.last_synced_at = timezone.now()
            for field, value in observation.items():
                setattr(correlation, field, value)
            correlation.save(
                update_fields=[
                    "google_event_id",
                    "payload_hash",
                    "calendar_generation",
                    "provider_etag",
                    "provider_payload_hash",
                    "provider_status",
                    "last_synced_at",
                    "updated_at",
                ]
            )
        _persist_refreshed_access_token(connection, client)
        return "created"

    provider_event_id = _client_event_id_from_recovery(
        client,
        connection,
        entity_id,
        correlation.google_event_id,
    )
    if provider_event_id is None:
        try:
            provider_event = client.insert_event(connection.calendar_id, deterministic_event_id, payload)
            provider_event_id = deterministic_event_id
        except GoogleCalendarClientConflict:
            provider_event_id = deterministic_event_id
            provider_event = _update_or_recreate_provider_event(
                client,
                connection.calendar_id,
                deterministic_event_id,
                provider_event_id,
                payload,
            )
    else:
        provider_event = _update_or_recreate_provider_event(
            client,
            connection.calendar_id,
            deterministic_event_id,
            provider_event_id,
            payload,
        )
        provider_event_id = _provider_event_id(provider_event, provider_event_id)
    observation = _provider_observation(provider_event, payload_hash)
    correlation.google_event_id = provider_event_id
    correlation.payload_hash = payload_hash
    correlation.last_synced_at = timezone.now()
    for field, value in observation.items():
        setattr(correlation, field, value)
    correlation.save(
        update_fields=[
            "google_event_id",
            "payload_hash",
            "provider_etag",
            "provider_payload_hash",
            "provider_status",
            "last_synced_at",
            "updated_at",
        ]
    )
    _persist_refreshed_access_token(connection, client)
    return "updated"


def _provider_event_id(provider_event, fallback):
    if isinstance(provider_event, dict):
        event_id = provider_event.get("id")
        if isinstance(event_id, str) and event_id:
            return event_id
    return fallback


def _provider_observation(provider_event, payload_hash):
    etag = provider_event.get("etag", "") if isinstance(provider_event, dict) else ""
    status = provider_event.get("status", "confirmed") if isinstance(provider_event, dict) else "confirmed"
    observed_payload_hash = payload_hash
    if isinstance(provider_event, dict) and any(
        key in provider_event
        for key in ("summary", "description", "start", "end", "reminders", "extendedProperties", "colorId")
    ):
        observed_payload_hash = google_calendar_provider_payload_hash(provider_event)
    return {
        "provider_etag": etag if isinstance(etag, str) else "",
        "provider_payload_hash": observed_payload_hash,
        "provider_status": status if isinstance(status, str) else "confirmed",
    }


def _update_or_recreate_provider_event(client, calendar_id, deterministic_event_id, provider_event_id, payload):
    try:
        return client.update_event(calendar_id, provider_event_id, payload)
    except (GoogleCalendarClientConflict, GoogleCalendarEventAbsent):
        existing = client.get_event(calendar_id, provider_event_id)
        if existing is not None:
            return client.update_event(calendar_id, provider_event_id, payload)
        try:
            return client.insert_event(calendar_id, deterministic_event_id, payload)
        except GoogleCalendarClientConflict:
            existing = client.get_event(calendar_id, deterministic_event_id)
            if existing is None:
                return client.insert_event(calendar_id, deterministic_event_id, payload)
            return client.update_event(calendar_id, deterministic_event_id, payload)


@transaction.atomic
def _synchronize_issue_for_connection_transaction(issue_id, connection_id):
    try:
        connection = (
            GoogleCalendarConnection.objects.select_for_update()
            .select_related("workspace_integration")
            .get(id=connection_id)
        )
    except GoogleCalendarConnection.DoesNotExist:
        return "missing_connection"

    correlation = GoogleCalendarEvent.objects.filter(
        connection=connection,
        entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
        entity_id=issue_id,
    ).first()
    try:
        issue = _issue_queryset().get(id=issue_id)
    except Issue.DoesNotExist:
        if correlation is None:
            return "missing"
        delete_result = _delete_provider_event(connection, correlation)
        if isinstance(delete_result, _GoogleCalendarProviderRetry):
            return delete_result
        if not delete_result:
            return "credential_mismatch"
        return "deleted"

    eligible = is_issue_assignment_eligible(issue, connection)
    update_terminal = bool(connection.workspace_integration.config.get("update_on_completion", True))
    terminal = issue.state and issue.state.group in {StateGroup.COMPLETED, StateGroup.CANCELLED}
    if not eligible or (terminal and not update_terminal):
        if correlation is None:
            return "ineligible"
        delete_result = _delete_provider_event(connection, correlation)
        if isinstance(delete_result, _GoogleCalendarProviderRetry):
            return delete_result
        if not delete_result:
            return "credential_mismatch"
        return "deleted"

    payload = build_google_calendar_work_item_event(issue)
    return _converge_provider_event(
        connection,
        GoogleCalendarEvent.EntityType.WORK_ITEM,
        issue.id,
        payload,
        correlation,
    )


def _synchronize_issue_for_connection(issue_id, connection_id):
    result = _synchronize_issue_for_connection_transaction(issue_id, connection_id)
    if isinstance(result, _GoogleCalendarProviderRetry):
        raise result.error
    return result


def _connection_ids_for_issue(issue, connection_id=None):
    if connection_id is not None:
        return [UUID(str(connection_id))]

    assignee_ids = IssueAssignee.objects.filter(issue_id=issue.id).values_list("assignee_id", flat=True)
    eligible_connection_ids = GoogleCalendarConnection.objects.filter(
        workspace_integration__workspace_id=issue.workspace_id,
        member_id__in=assignee_ids,
    ).values_list("id", flat=True)
    correlated_connection_ids = GoogleCalendarEvent.objects.filter(
        entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
        entity_id=issue.id,
    ).values_list("connection_id", flat=True)
    return sorted(set(eligible_connection_ids) | set(correlated_connection_ids), key=str)


@shared_task
def synchronize_google_calendar_issue(issue_id, connection_id=None):
    """Converge one work item for all relevant assignee connections."""

    try:
        issue = _issue_queryset().get(id=issue_id)
    except Issue.DoesNotExist:
        if connection_id is not None:
            target_connection_ids = [UUID(str(connection_id))]
        else:
            target_connection_ids = list(
                GoogleCalendarEvent.objects.filter(
                    entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
                    entity_id=issue_id,
                ).values_list("connection_id", flat=True)
            )
        for target_connection_id in target_connection_ids:
            _synchronize_issue_for_connection(issue_id, target_connection_id)
        return "missing"

    results = [
        _synchronize_issue_for_connection(issue.id, target_connection_id)
        for target_connection_id in _connection_ids_for_issue(issue, connection_id)
    ]
    return results


@transaction.atomic
def _synchronize_cycle_for_connection_transaction(cycle_id, connection_id):
    try:
        connection = (
            GoogleCalendarConnection.objects.select_for_update()
            .select_related("workspace_integration")
            .get(id=connection_id)
        )
    except GoogleCalendarConnection.DoesNotExist:
        return "missing_connection"

    correlation = GoogleCalendarEvent.objects.filter(
        connection=connection,
        entity_type=GoogleCalendarEvent.EntityType.CYCLE,
        entity_id=cycle_id,
    ).first()
    try:
        cycle = _cycle_queryset().get(id=cycle_id)
    except Cycle.DoesNotExist:
        if correlation is None:
            return "missing"
        delete_result = _delete_provider_event(connection, correlation)
        if isinstance(delete_result, _GoogleCalendarProviderRetry):
            return delete_result
        if not delete_result:
            return "credential_mismatch"
        return "deleted"

    recipient_connection_ids = []
    retained_recipient_connection_ids = []
    if is_cycle_sync_enabled(cycle):
        recipient_connection_ids = cycle_recipient_connection_ids(cycle)
        retained_recipient_connection_ids = cycle_recipient_connection_ids(cycle, healthy_only=False)
    creatable = is_cycle_creatable(
        cycle,
        connection,
        recipient_connection_ids=recipient_connection_ids,
    )
    retained = correlation is not None and should_retain_cycle_event(
        cycle,
        connection,
        recipient_connection_ids=retained_recipient_connection_ids,
    )
    if retained:
        return "retained"
    if not creatable:
        if correlation is None:
            return "ineligible"
        delete_result = _delete_provider_event(connection, correlation)
        if isinstance(delete_result, _GoogleCalendarProviderRetry):
            return delete_result
        if not delete_result:
            return "credential_mismatch"
        return "deleted"

    payload = build_google_calendar_cycle_event(cycle)
    return _converge_provider_event(
        connection,
        GoogleCalendarEvent.EntityType.CYCLE,
        cycle.id,
        payload,
        correlation,
    )


def _synchronize_cycle_for_connection(cycle_id, connection_id):
    result = _synchronize_cycle_for_connection_transaction(cycle_id, connection_id)
    if isinstance(result, _GoogleCalendarProviderRetry):
        raise result.error
    return result


def _connection_ids_for_cycle(cycle, connection_id=None):
    if connection_id is not None:
        return [UUID(str(connection_id))]

    recipient_connection_ids = []
    if is_cycle_sync_enabled(cycle):
        recipient_connection_ids = cycle_recipient_connection_ids(cycle)
    correlated_connection_ids = GoogleCalendarEvent.objects.filter(
        entity_type=GoogleCalendarEvent.EntityType.CYCLE,
        entity_id=cycle.id,
    ).values_list("connection_id", flat=True)
    return sorted(set(recipient_connection_ids) | set(correlated_connection_ids), key=str)


def _connection_ids_for_issue_id(issue_id):
    try:
        issue = _issue_queryset().get(id=issue_id)
    except Issue.DoesNotExist:
        return list(
            GoogleCalendarEvent.objects.filter(
                entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
                entity_id=issue_id,
            ).values_list("connection_id", flat=True)
        )
    return _connection_ids_for_issue(issue)


def _connection_ids_for_cycle_id(cycle_id):
    try:
        cycle = _cycle_queryset().get(id=cycle_id)
    except Cycle.DoesNotExist:
        return list(
            GoogleCalendarEvent.objects.filter(
                entity_type=GoogleCalendarEvent.EntityType.CYCLE,
                entity_id=cycle_id,
            ).values_list("connection_id", flat=True)
        )
    return _connection_ids_for_cycle(cycle)


def _paced_google_calendar_sync_tasks(entity_ids, task_name, connection_ids_for_entity):
    """Build targeted tasks whose countdown advances independently for each connection."""

    next_countdown_by_connection = {}
    for entity_id in dict.fromkeys(entity_ids):
        for connection_id in connection_ids_for_entity(entity_id):
            countdown = next_countdown_by_connection.get(connection_id, 0)
            task = current_app.signature(task_name).set(countdown=countdown)
            yield task, str(entity_id), str(connection_id)
            next_countdown_by_connection[connection_id] = countdown + 1


def paced_google_calendar_issue_sync_tasks(issue_ids):
    """Build a bounded issue fan-out with one independent sequence per connection."""

    return _paced_google_calendar_sync_tasks(
        issue_ids,
        GOOGLE_CALENDAR_ISSUE_SYNC_TASK,
        _connection_ids_for_issue_id,
    )


def paced_google_calendar_cycle_sync_tasks(cycle_ids):
    """Build a bounded cycle fan-out with one independent sequence per connection."""

    return _paced_google_calendar_sync_tasks(
        cycle_ids,
        GOOGLE_CALENDAR_CYCLE_SYNC_TASK,
        _connection_ids_for_cycle_id,
    )


@shared_task
def synchronize_google_calendar_cycle(cycle_id, connection_id=None):
    """Converge one cycle block for every current or previously correlated recipient."""

    try:
        cycle = _cycle_queryset().get(id=cycle_id)
    except Cycle.DoesNotExist:
        if connection_id is not None:
            target_connection_ids = [UUID(str(connection_id))]
        else:
            target_connection_ids = list(
                GoogleCalendarEvent.objects.filter(
                    entity_type=GoogleCalendarEvent.EntityType.CYCLE,
                    entity_id=cycle_id,
                ).values_list("connection_id", flat=True)
            )
        for target_connection_id in target_connection_ids:
            _synchronize_cycle_for_connection(cycle_id, target_connection_id)
        return "missing"

    return [
        _synchronize_cycle_for_connection(cycle.id, target_connection_id)
        for target_connection_id in _connection_ids_for_cycle(cycle, connection_id)
    ]


@shared_task
def backfill_google_calendar_cycles(connection_id, after_id=None, batch_size=100):
    """Publish a bounded batch of active and upcoming cycles for one connection."""

    try:
        connection = GoogleCalendarConnection.objects.select_related("workspace_integration").get(id=connection_id)
    except GoogleCalendarConnection.DoesNotExist:
        return "missing"

    queryset = _cycle_queryset().filter(workspace_id=connection.workspace_integration.workspace_id).order_by("id")
    if after_id is not None:
        queryset = queryset.filter(id__gt=after_id)
    cycles = list(queryset[: int(batch_size) + 1])
    current_batch = cycles[: int(batch_size)]

    published = 0
    for index, cycle in enumerate(current_batch):
        if not is_cycle_creatable(cycle, connection):
            continue
        sync_task = current_app.signature(GOOGLE_CALENDAR_CYCLE_SYNC_TASK).set(countdown=index)
        enqueue_google_calendar_task_on_commit(sync_task, str(cycle.id), str(connection.id))
        published += 1

    if len(cycles) > len(current_batch) and current_batch:
        continuation = current_app.signature(GOOGLE_CALENDAR_CYCLE_BACKFILL_TASK).set(countdown=len(current_batch))
        enqueue_google_calendar_task_on_commit(
            continuation,
            str(connection.id),
            str(current_batch[-1].id),
            int(batch_size),
        )
    return published


@shared_task
def backfill_google_calendar_open_issues(connection_id, after_id=None, batch_size=100):
    """Publish a bounded, paced batch of current open work items for one connection."""

    try:
        connection = GoogleCalendarConnection.objects.select_related("workspace_integration").get(id=connection_id)
    except GoogleCalendarConnection.DoesNotExist:
        return "missing"

    queryset = _issue_queryset().filter(
        workspace_id=connection.workspace_integration.workspace_id,
        target_date__isnull=False,
    )
    policy = connection.workspace_integration.config or {}
    if policy.get("mode", "assignment") != "filter":
        queryset = queryset.filter(issue_assignee__assignee_id=connection.member_id)
    queryset = queryset.distinct().order_by("id")
    if after_id is not None:
        queryset = queryset.filter(id__gt=after_id)
    issues = list(queryset[: int(batch_size) + 1])
    current_batch = issues[: int(batch_size)]

    published = 0
    for index, issue in enumerate(current_batch):
        if not is_issue_open_backfill_eligible(issue, connection):
            continue
        sync_task = current_app.signature(GOOGLE_CALENDAR_ISSUE_SYNC_TASK).set(countdown=index)
        enqueue_google_calendar_task_on_commit(sync_task, str(issue.id), str(connection.id))
        published += 1

    if len(issues) > len(current_batch) and current_batch:
        # The continuation starts a fresh countdown sequence, so place it after this page's final pacing slot.
        next_batch = current_app.signature(GOOGLE_CALENDAR_OPEN_BACKFILL_TASK).set(countdown=len(current_batch))
        enqueue_google_calendar_task_on_commit(
            next_batch,
            str(connection.id),
            str(current_batch[-1].id),
            int(batch_size),
        )
    return published


@shared_task
def resync_google_calendar_state_issues(state_id, after_id=None, batch_size=100):
    """Publish a bounded convergence batch for issues using one saved State."""

    issue_ids = Issue.all_objects.filter(state_id=state_id).order_by("id")
    if after_id is not None:
        issue_ids = issue_ids.filter(id__gt=after_id)
    issue_ids = list(issue_ids.values_list("id", flat=True)[: int(batch_size) + 1])
    current_batch = issue_ids[: int(batch_size)]

    # Import locally because the signal module owns this shared dispatch boundary
    # and imports the task definitions while Django initializes receivers.
    from plane.db.signals import dispatch_google_calendar_issue_syncs

    dispatch_google_calendar_issue_syncs(current_batch)

    if len(issue_ids) > len(current_batch) and current_batch:
        continuation = current_app.signature(GOOGLE_CALENDAR_STATE_ISSUE_RESYNC_TASK).set(countdown=len(current_batch))
        publish_google_calendar_task(
            continuation,
            str(state_id),
            str(current_batch[-1]),
            int(batch_size),
        )
    return len(current_batch)


@shared_task
def resync_google_calendar_label(label_id, after_id=None, batch_size=100):
    """Publish a bounded convergence batch for issues linked to one saved Label."""

    issue_ids = IssueLabel.all_objects.filter(label_id=label_id).order_by("issue_id")
    if after_id is not None:
        issue_ids = issue_ids.filter(issue_id__gt=after_id)
    issue_ids = list(issue_ids.values_list("issue_id", flat=True).distinct()[: int(batch_size) + 1])
    current_batch = issue_ids[: int(batch_size)]

    # Import locally because the signal module owns this shared dispatch boundary
    # and imports the task definitions while Django initializes receivers.
    from plane.db.signals import dispatch_google_calendar_issue_syncs

    dispatch_google_calendar_issue_syncs(current_batch)

    if len(issue_ids) > len(current_batch) and current_batch:
        continuation = current_app.signature(GOOGLE_CALENDAR_LABEL_ISSUE_RESYNC_TASK).set(countdown=len(current_batch))
        publish_google_calendar_task(
            continuation,
            str(label_id),
            str(current_batch[-1]),
            int(batch_size),
        )
    return len(current_batch)


@shared_task
def resync_google_calendar_project_issues(project_id, after_id=None, batch_size=100):
    """Publish a bounded convergence batch for one project's work items."""

    try:
        project = Project.all_objects.get(id=project_id)
    except Project.DoesNotExist:
        return "missing"

    issue_ids = Issue.all_objects.filter(project_id=project.id).order_by("id")
    if after_id is not None:
        issue_ids = issue_ids.filter(id__gt=after_id)
    issue_ids = list(issue_ids.values_list("id", flat=True)[: int(batch_size) + 1])
    current_batch = issue_ids[: int(batch_size)]

    for sync_task, issue_id, connection_id in paced_google_calendar_issue_sync_tasks(current_batch):
        publish_google_calendar_task(sync_task, issue_id, connection_id)

    if len(issue_ids) > len(current_batch) and current_batch:
        continuation = current_app.signature(GOOGLE_CALENDAR_PROJECT_ISSUE_RESYNC_TASK).set(
            countdown=len(current_batch)
        )
        publish_google_calendar_task(
            continuation,
            str(project.id),
            str(current_batch[-1]),
            int(batch_size),
        )
    else:
        cycle_resync = current_app.signature(GOOGLE_CALENDAR_PROJECT_CYCLE_RESYNC_TASK).set(
            countdown=len(current_batch)
        )
        publish_google_calendar_task(cycle_resync, str(project.id))
    return len(current_batch)


@shared_task
def resync_google_calendar_project_cycles(project_id, after_id=None, batch_size=100):
    """Publish a bounded convergence batch for one project's cycles."""

    try:
        project = Project.all_objects.get(id=project_id)
    except Project.DoesNotExist:
        return "missing"

    cycle_ids = Cycle.all_objects.filter(project_id=project.id).order_by("id")
    if after_id is not None:
        cycle_ids = cycle_ids.filter(id__gt=after_id)
    cycle_ids = list(cycle_ids.values_list("id", flat=True)[: int(batch_size) + 1])
    current_batch = cycle_ids[: int(batch_size)]

    for sync_task, cycle_id, connection_id in paced_google_calendar_cycle_sync_tasks(current_batch):
        publish_google_calendar_task(sync_task, cycle_id, connection_id)

    if len(cycle_ids) > len(current_batch) and current_batch:
        continuation = current_app.signature(GOOGLE_CALENDAR_PROJECT_CYCLE_RESYNC_TASK).set(
            countdown=len(current_batch)
        )
        publish_google_calendar_task(
            continuation,
            str(project.id),
            str(current_batch[-1]),
            int(batch_size),
        )
    return len(current_batch)


def _workspace_issue_ids_for_resync(workspace_id, after_id=None):
    issue_ids = Issue.all_objects.filter(workspace_id=workspace_id)
    correlation_issue_ids = GoogleCalendarEvent.objects.filter(
        connection__workspace_integration__workspace_id=workspace_id,
        entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
    )
    if after_id is not None:
        issue_ids = issue_ids.filter(id__gt=after_id)
        correlation_issue_ids = correlation_issue_ids.filter(entity_id__gt=after_id)
    return (
        issue_ids.order_by()
        .values_list("id", flat=True)
        .union(correlation_issue_ids.order_by().values_list("entity_id", flat=True))
        .order_by("id")
    )


def _workspace_issue_resync_generation(workspace_id):
    return _workspace_resync_generation(workspace_id, GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY)


def _workspace_cycle_ids_for_resync(workspace_id, after_id=None):
    cycle_ids = Cycle.all_objects.filter(workspace_id=workspace_id)
    correlation_cycle_ids = GoogleCalendarEvent.objects.filter(
        connection__workspace_integration__workspace_id=workspace_id,
        entity_type=GoogleCalendarEvent.EntityType.CYCLE,
    )
    if after_id is not None:
        cycle_ids = cycle_ids.filter(id__gt=after_id)
        correlation_cycle_ids = correlation_cycle_ids.filter(entity_id__gt=after_id)
    return (
        cycle_ids.order_by()
        .values_list("id", flat=True)
        .union(correlation_cycle_ids.order_by().values_list("entity_id", flat=True))
        .order_by("id")
    )


def _workspace_resync_generation(workspace_id, metadata_key):
    metadata = (
        WorkspaceIntegration.objects.filter(
            workspace_id=workspace_id,
            integration__provider="google_calendar",
        )
        .values_list("metadata", flat=True)
        .first()
        or {}
    )
    return metadata.get(metadata_key)


def _complete_workspace_issue_resync(workspace_id, policy_generation):
    return _complete_workspace_resync(
        workspace_id,
        policy_generation,
        GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY,
    )


@transaction.atomic
def _complete_workspace_resync(workspace_id, policy_generation, metadata_key):
    workspace_integration = (
        WorkspaceIntegration.objects.select_for_update()
        .filter(
            workspace_id=workspace_id,
            integration__provider="google_calendar",
        )
        .first()
    )
    if workspace_integration is None:
        return False
    metadata = dict(workspace_integration.metadata or {})
    if metadata.get(metadata_key) != policy_generation:
        return False
    metadata.pop(metadata_key)
    workspace_integration.metadata = metadata
    workspace_integration.save(update_fields=["metadata", "updated_at"])
    return True


@shared_task(bind=True, max_retries=12)
def resync_google_calendar_workspace_issues(
    task,
    workspace_id,
    after_id=None,
    batch_size=100,
    policy_generation=None,
):
    """Publish a bounded, paced convergence pass for every workspace work item."""

    if policy_generation is not None and _workspace_issue_resync_generation(workspace_id) != policy_generation:
        return "stale"

    if GoogleCalendarConnection.objects.filter(
        workspace_integration__workspace_id=workspace_id,
        desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
        status=GoogleCalendarConnection.Status.PENDING,
    ).exists():
        raise task.retry(countdown=5)

    issue_ids = list(_workspace_issue_ids_for_resync(workspace_id, after_id)[: int(batch_size) + 1])
    current_batch = issue_ids[: int(batch_size)]

    try:
        for sync_task, issue_id, connection_id in paced_google_calendar_issue_sync_tasks(current_batch):
            publish_google_calendar_task(sync_task, issue_id, connection_id)

        if len(issue_ids) > len(current_batch) and current_batch:
            next_batch = current_app.signature(GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK).set(
                countdown=len(current_batch)
            )
            publish_google_calendar_task(
                next_batch,
                str(workspace_id),
                str(current_batch[-1]),
                int(batch_size),
                policy_generation=policy_generation,
            )
        elif policy_generation is not None:
            _complete_workspace_issue_resync(workspace_id, policy_generation)
    except Exception as exc:
        raise task.retry(exc=exc, countdown=5) from exc
    return len(current_batch)


@shared_task(bind=True, max_retries=12)
def resync_google_calendar_workspace_cycles(
    task,
    workspace_id,
    after_id=None,
    batch_size=100,
    policy_generation=None,
):
    """Publish a bounded, paced convergence pass for every workspace cycle."""

    if (
        policy_generation is not None
        and _workspace_resync_generation(
            workspace_id,
            GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_METADATA_KEY,
        )
        != policy_generation
    ):
        return "stale"

    if GoogleCalendarConnection.objects.filter(
        workspace_integration__workspace_id=workspace_id,
        desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
        status=GoogleCalendarConnection.Status.PENDING,
    ).exists():
        raise task.retry(countdown=5)

    cycle_ids = list(_workspace_cycle_ids_for_resync(workspace_id, after_id)[: int(batch_size) + 1])
    current_batch = cycle_ids[: int(batch_size)]

    try:
        for sync_task, cycle_id, connection_id in paced_google_calendar_cycle_sync_tasks(current_batch):
            publish_google_calendar_task(sync_task, cycle_id, connection_id)

        if len(cycle_ids) > len(current_batch) and current_batch:
            next_batch = current_app.signature(GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_TASK).set(
                countdown=len(current_batch)
            )
            publish_google_calendar_task(
                next_batch,
                str(workspace_id),
                str(current_batch[-1]),
                int(batch_size),
                policy_generation=policy_generation,
            )
        elif policy_generation is not None:
            _complete_workspace_resync(
                workspace_id,
                policy_generation,
                GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_METADATA_KEY,
            )
    except Exception as exc:
        raise task.retry(exc=exc, countdown=5) from exc
    return len(current_batch)


@shared_task(bind=True, max_retries=12)
def reconcile_google_calendar_workspace_issue_resyncs(task, after_id=None, batch_size=100):
    """Rediscover and publish durable workspace policy resync requests."""

    workspace_integrations = (
        WorkspaceIntegration.objects.filter(
            integration__provider="google_calendar",
        )
        .filter(
            Q(metadata__has_key=GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY)
            | Q(metadata__has_key=GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_METADATA_KEY)
        )
        .order_by("id")
    )
    if after_id is not None:
        workspace_integrations = workspace_integrations.filter(id__gt=after_id)
    pending = list(workspace_integrations[: int(batch_size) + 1])
    current_batch = pending[: int(batch_size)]

    try:
        for workspace_integration in current_batch:
            pending_lifecycle_generations = (
                GoogleCalendarConnection.objects.filter(
                    workspace_integration=workspace_integration,
                    desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
                    status=GoogleCalendarConnection.Status.PENDING,
                )
                .order_by("id")
                .values_list("id", "lifecycle_generation")
            )
            for connection_id, lifecycle_generation in pending_lifecycle_generations:
                lifecycle_task = current_app.signature(GOOGLE_CALENDAR_LIFECYCLE_TASK)
                publish_google_calendar_task(
                    lifecycle_task,
                    str(connection_id),
                    lifecycle_generation,
                )
            issue_generation = workspace_integration.metadata.get(GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_METADATA_KEY)
            if issue_generation is not None:
                issue_resync_task = current_app.signature(GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK)
                publish_google_calendar_task(
                    issue_resync_task,
                    str(workspace_integration.workspace_id),
                    policy_generation=issue_generation,
                )
            cycle_generation = workspace_integration.metadata.get(GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_METADATA_KEY)
            if cycle_generation is not None:
                cycle_resync_task = current_app.signature(GOOGLE_CALENDAR_WORKSPACE_CYCLE_RESYNC_TASK)
                publish_google_calendar_task(
                    cycle_resync_task,
                    str(workspace_integration.workspace_id),
                    policy_generation=cycle_generation,
                )

        if len(pending) > len(current_batch) and current_batch:
            continuation = current_app.signature(GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_RECONCILIATION_TASK)
            publish_google_calendar_task(
                continuation,
                str(current_batch[-1].id),
                int(batch_size),
            )
    except Exception as exc:
        raise task.retry(exc=exc, countdown=5) from exc
    return len(current_batch)


def _reconciliation_state(connection):
    try:
        state = json.loads(connection.reconciliation_cursor or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get("run_id"), str):
        return None
    return state


def _save_reconciliation_state(connection, state, *, phase=None, page_token=None):
    connection.reconciliation_cursor = json.dumps(state, separators=(",", ":"), sort_keys=True)
    connection.reconciliation_lease_expires_at = timezone.now() + GOOGLE_CALENDAR_RECONCILIATION_LEASE
    update_fields = ["reconciliation_cursor", "reconciliation_lease_expires_at", "updated_at"]
    if phase is not None:
        connection.reconciliation_phase = phase
        update_fields.append("reconciliation_phase")
    if page_token is not None:
        connection.page_token = page_token
        update_fields.append("page_token")
    connection.save(update_fields=update_fields)


def _owns_reconciliation_lease(connection, state, run_id, lease_token, *, phase=None):
    return (
        state is not None
        and state["run_id"] == str(run_id)
        and bool(lease_token)
        and state.get("lease_token") == str(lease_token)
        and (phase is None or connection.reconciliation_phase == phase)
        and state.get("calendar_generation") == connection.calendar_generation
        and state.get("lifecycle_generation") == connection.lifecycle_generation
    )


def _claim_reconciliation_lease(connection, state):
    state["lease_token"] = str(uuid4())
    _save_reconciliation_state(connection, state)


@transaction.atomic
def _start_or_resume_inventory(connection_id, run_id, force_local_scan):
    try:
        connection = (
            GoogleCalendarConnection.objects.select_for_update()
            .select_related("workspace_integration")
            .get(id=connection_id)
        )
    except GoogleCalendarConnection.DoesNotExist:
        return "missing"
    if (
        connection.desired_state != GoogleCalendarConnection.DesiredState.CONNECTED
        or connection.status not in {GoogleCalendarConnection.Status.ACTIVE, GoogleCalendarConnection.Status.PENDING}
        or not connection.calendar_id
    ):
        return "inactive"

    state = _reconciliation_state(connection)
    if run_id is not None:
        if state is None or state["run_id"] != str(run_id):
            return "stale"
        if (
            state.get("calendar_generation") != connection.calendar_generation
            or state.get("lifecycle_generation") != connection.lifecycle_generation
        ):
            return "stale"
        if connection.reconciliation_lease_expires_at and connection.reconciliation_lease_expires_at > timezone.now():
            return "leased"
        _claim_reconciliation_lease(connection, state)
        return connection

    if (
        connection.reconciliation_phase
        and connection.reconciliation_lease_expires_at
        and connection.reconciliation_lease_expires_at > timezone.now()
    ):
        return "leased"
    if (
        connection.reconciliation_phase
        and state is not None
        and state.get("calendar_generation") == connection.calendar_generation
        and state.get("lifecycle_generation") == connection.lifecycle_generation
    ):
        _claim_reconciliation_lease(connection, state)
        return connection

    full_inventory = not bool(connection.sync_token)
    state = {
        "run_id": str(uuid4()),
        "lease_token": str(uuid4()),
        "calendar_generation": connection.calendar_generation,
        "lifecycle_generation": connection.lifecycle_generation,
        "after_id": "",
        "force_local_scan": bool(force_local_scan),
        "full_inventory": full_inventory,
        "saw_delta": False,
    }
    connection.reconciliation_completed_at = None
    connection.reconciliation_phase = GOOGLE_CALENDAR_RECONCILIATION_PROVIDER_PHASE
    connection.page_token = ""
    connection.reconciliation_cursor = json.dumps(state, separators=(",", ":"), sort_keys=True)
    connection.reconciliation_lease_expires_at = timezone.now() + GOOGLE_CALENDAR_RECONCILIATION_LEASE
    connection.save(
        update_fields=[
            "reconciliation_completed_at",
            "reconciliation_phase",
            "page_token",
            "reconciliation_cursor",
            "reconciliation_lease_expires_at",
            "updated_at",
        ]
    )
    if full_inventory:
        GoogleCalendarEvent.objects.filter(
            connection=connection,
            calendar_generation=connection.calendar_generation,
        ).update(provider_etag="", provider_payload_hash="", provider_status="")
    return connection


def _provider_marker(event):
    extended_properties = event.get("extendedProperties")
    if not isinstance(extended_properties, dict):
        return None
    private = extended_properties.get("private")
    if not isinstance(private, dict):
        return None
    entity_type = private.get("plane_entity_type")
    entity_id = private.get("plane_entity_id")
    if entity_type not in GoogleCalendarEvent.EntityType.values or not isinstance(entity_id, str):
        return None
    try:
        return entity_type, UUID(entity_id)
    except ValueError:
        return None


@transaction.atomic
def _record_inventory_page(connection_id, run_id, lease_token, page):
    connection = GoogleCalendarConnection.objects.select_for_update().get(id=connection_id)
    state = _reconciliation_state(connection)
    if not _owns_reconciliation_lease(
        connection,
        state,
        run_id,
        lease_token,
        phase=GOOGLE_CALENDAR_RECONCILIATION_PROVIDER_PHASE,
    ):
        return "stale"

    event_ids = [event.get("id") for event in page.events if isinstance(event.get("id"), str)]
    correlations = list(
        GoogleCalendarEvent.objects.filter(
            connection=connection,
            calendar_generation=connection.calendar_generation,
            google_event_id__in=event_ids,
        )
    )
    by_provider_id = {correlation.google_event_id: correlation for correlation in correlations}
    marker_keys = [_provider_marker(event) for event in page.events]
    valid_marker_keys = [marker for marker in marker_keys if marker is not None]
    by_marker = {
        (correlation.entity_type, correlation.entity_id): correlation
        for correlation in GoogleCalendarEvent.objects.filter(
            connection=connection,
            calendar_generation=connection.calendar_generation,
        ).filter(
            Q(
                entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
                entity_id__in=[
                    key[1] for key in valid_marker_keys if key[0] == GoogleCalendarEvent.EntityType.WORK_ITEM
                ],
            )
            | Q(
                entity_type=GoogleCalendarEvent.EntityType.CYCLE,
                entity_id__in=[key[1] for key in valid_marker_keys if key[0] == GoogleCalendarEvent.EntityType.CYCLE],
            )
        )
    }

    changed = []
    for event, marker in zip(page.events, marker_keys, strict=True):
        event_id = event.get("id")
        correlation = by_provider_id.get(event_id)
        if correlation is None and marker is not None:
            correlation = by_marker.get(marker)
        if correlation is None:
            continue
        etag = event.get("etag", "")
        status = event.get("status", "")
        provider_hash = google_calendar_provider_payload_hash(event)
        if not isinstance(etag, str):
            etag = ""
        if not isinstance(status, str):
            status = ""
        if (
            correlation.google_event_id == event_id
            and correlation.provider_etag == etag
            and correlation.provider_payload_hash == provider_hash
            and correlation.provider_status == status
        ):
            continue
        if isinstance(event_id, str) and event_id:
            correlation.google_event_id = event_id
        correlation.provider_etag = etag
        correlation.provider_payload_hash = provider_hash
        correlation.provider_status = status
        changed.append(correlation)
    if changed:
        GoogleCalendarEvent.objects.bulk_update(
            changed,
            ["google_event_id", "provider_etag", "provider_payload_hash", "provider_status"],
        )

    state["saw_delta"] = state.get("saw_delta", False) or bool(page.events)
    if page.next_page_token:
        _save_reconciliation_state(connection, state, page_token=page.next_page_token)
        return "more"

    connection.sync_token = page.next_sync_token or connection.sync_token
    connection.page_token = ""
    connection.save(update_fields=["sync_token", "page_token", "updated_at"])
    needs_local_scan = bool(state.get("force_local_scan") or state.get("full_inventory") or state.get("saw_delta"))
    if needs_local_scan:
        _save_reconciliation_state(
            connection,
            state,
            phase=GOOGLE_CALENDAR_RECONCILIATION_LOCAL_PHASE,
            page_token="",
        )
        return "local"
    return _complete_reconciliation_run(connection, state)


@transaction.atomic
def _expire_inventory_sync_token(connection_id, run_id, lease_token):
    connection = GoogleCalendarConnection.objects.select_for_update().get(id=connection_id)
    state = _reconciliation_state(connection)
    if not _owns_reconciliation_lease(
        connection,
        state,
        run_id,
        lease_token,
        phase=GOOGLE_CALENDAR_RECONCILIATION_PROVIDER_PHASE,
    ):
        return False
    connection.sync_token = ""
    connection.page_token = ""
    state["full_inventory"] = True
    GoogleCalendarEvent.objects.filter(
        connection=connection,
        calendar_generation=connection.calendar_generation,
    ).update(provider_etag="", provider_payload_hash="", provider_status="")
    connection.save(update_fields=["sync_token", "page_token", "updated_at"])
    _save_reconciliation_state(connection, state, page_token="")
    return True


def _publish_reconciliation_continuation(connection_id, run_id, countdown=None):
    continuation = current_app.signature(GOOGLE_CALENDAR_INVENTORY_TASK)
    if countdown is not None:
        continuation = continuation.set(countdown=countdown)
    publish_google_calendar_task(continuation, str(connection_id), str(run_id))


@transaction.atomic
def _release_reconciliation_lease(connection_id, run_id, lease_token):
    connection = GoogleCalendarConnection.objects.select_for_update().get(id=connection_id)
    state = _reconciliation_state(connection)
    if not _owns_reconciliation_lease(connection, state, run_id, lease_token):
        return False
    state["lease_token"] = ""
    connection.reconciliation_cursor = json.dumps(state, separators=(",", ":"), sort_keys=True)
    connection.reconciliation_lease_expires_at = None
    connection.save(update_fields=["reconciliation_cursor", "reconciliation_lease_expires_at", "updated_at"])
    return True


@transaction.atomic
def _request_calendar_replacement(connection_id, run_id, lease_token):
    connection = GoogleCalendarConnection.objects.select_for_update().get(id=connection_id)
    state = _reconciliation_state(connection)
    if not _owns_reconciliation_lease(
        connection,
        state,
        run_id,
        lease_token,
        phase=GOOGLE_CALENDAR_RECONCILIATION_PROVIDER_PHASE,
    ):
        return "stale"
    connection.status = GoogleCalendarConnection.Status.PENDING
    connection.reconciliation_phase = ""
    connection.reconciliation_cursor = ""
    connection.reconciliation_lease_expires_at = None
    connection.save(
        update_fields=[
            "status",
            "reconciliation_phase",
            "reconciliation_cursor",
            "reconciliation_lease_expires_at",
            "updated_at",
        ]
    )
    lifecycle_task = current_app.signature(GOOGLE_CALENDAR_LIFECYCLE_TASK)
    enqueue_google_calendar_task_on_commit(
        lifecycle_task,
        str(connection.id),
        connection.lifecycle_generation,
    )
    return "replacement_pending"


@transaction.atomic
def _record_reconciliation_credential_mismatch(connection_id, run_id, lease_token):
    connection = GoogleCalendarConnection.objects.select_for_update().get(id=connection_id)
    state = _reconciliation_state(connection)
    if not _owns_reconciliation_lease(
        connection,
        state,
        run_id,
        lease_token,
        phase=GOOGLE_CALENDAR_RECONCILIATION_PROVIDER_PHASE,
    ):
        return False
    _mark_credential_mismatch(connection)
    return True


def _publish_correlation_sync(correlation, countdown):
    if correlation.entity_type == GoogleCalendarEvent.EntityType.WORK_ITEM:
        task_name = GOOGLE_CALENDAR_ISSUE_SYNC_TASK
    else:
        task_name = GOOGLE_CALENDAR_CYCLE_SYNC_TASK
    sync_task = current_app.signature(task_name).set(countdown=countdown)
    publish_google_calendar_task(sync_task, str(correlation.entity_id), str(correlation.connection_id))


@transaction.atomic
def _advance_local_scan(connection_id, run_id, lease_token):
    connection = GoogleCalendarConnection.objects.select_for_update().get(id=connection_id)
    state = _reconciliation_state(connection)
    if not _owns_reconciliation_lease(
        connection,
        state,
        run_id,
        lease_token,
        phase=GOOGLE_CALENDAR_RECONCILIATION_LOCAL_PHASE,
    ):
        return "stale", []
    queryset = GoogleCalendarEvent.objects.filter(
        connection=connection,
        calendar_generation=connection.calendar_generation,
    ).order_by("id")
    after_id = state.get("after_id")
    if after_id:
        queryset = queryset.filter(id__gt=after_id)
    correlations = list(queryset[: GOOGLE_CALENDAR_RECONCILIATION_LOCAL_PAGE_SIZE + 1])
    current_page = correlations[:GOOGLE_CALENDAR_RECONCILIATION_LOCAL_PAGE_SIZE]
    has_more = len(correlations) > len(current_page)
    if has_more:
        return "more", current_page
    connection.reconciliation_lease_expires_at = timezone.now() + GOOGLE_CALENDAR_RECONCILIATION_LEASE
    connection.save(update_fields=["reconciliation_lease_expires_at", "updated_at"])
    return "complete", current_page


@transaction.atomic
def _persist_local_scan_cursor(connection_id, run_id, lease_token, after_id):
    connection = GoogleCalendarConnection.objects.select_for_update().get(id=connection_id)
    state = _reconciliation_state(connection)
    if not _owns_reconciliation_lease(
        connection,
        state,
        run_id,
        lease_token,
        phase=GOOGLE_CALENDAR_RECONCILIATION_LOCAL_PHASE,
    ):
        return False
    state["after_id"] = str(after_id)
    _save_reconciliation_state(connection, state)
    return True


def _complete_reconciliation_run(connection, state):
    connection.reconciliation_phase = ""
    connection.reconciliation_cursor = ""
    connection.reconciliation_lease_expires_at = None
    connection.reconciliation_completed_at = timezone.now()
    connection.save(
        update_fields=[
            "reconciliation_phase",
            "reconciliation_cursor",
            "reconciliation_lease_expires_at",
            "reconciliation_completed_at",
            "updated_at",
        ]
    )
    return "complete"


@transaction.atomic
def _finish_reconciliation(connection_id, run_id, lease_token):
    connection = GoogleCalendarConnection.objects.select_for_update().get(id=connection_id)
    state = _reconciliation_state(connection)
    if not _owns_reconciliation_lease(
        connection,
        state,
        run_id,
        lease_token,
        phase=GOOGLE_CALENDAR_RECONCILIATION_LOCAL_PHASE,
    ):
        return "stale"
    return _complete_reconciliation_run(connection, state)


@shared_task
def reconcile_google_calendar_inventory(connection_id, run_id=None, force_local_scan=False):
    """Reconcile one connection within five provider pages or one 1,000-row local page."""

    connection = _start_or_resume_inventory(connection_id, run_id, force_local_scan)
    if isinstance(connection, str):
        return connection
    state = _reconciliation_state(connection)
    run_id = state["run_id"]
    lease_token = state["lease_token"]

    if connection.reconciliation_phase == GOOGLE_CALENDAR_RECONCILIATION_PROVIDER_PHASE:
        client = _client_for(connection)
        for _ in range(GOOGLE_CALENDAR_RECONCILIATION_PROVIDER_PAGE_LIMIT):
            connection.refresh_from_db()
            state = _reconciliation_state(connection)
            if not _owns_reconciliation_lease(connection, state, run_id, lease_token):
                return "stale"
            try:
                page = client.list_event_page(
                    connection.calendar_id,
                    page_token=connection.page_token or None,
                    sync_token=connection.sync_token or None,
                )
            except GoogleCalendarSyncTokenExpired:
                if not _expire_inventory_sync_token(connection.id, run_id, lease_token):
                    return "stale"
                connection.refresh_from_db()
                continue
            except GoogleCalendarCalendarAbsent:
                return _request_calendar_replacement(connection.id, run_id, lease_token)
            except GoogleCalendarCredentialMismatch:
                if not _record_reconciliation_credential_mismatch(connection.id, run_id, lease_token):
                    return "stale"
                return "credential_mismatch"
            result = _record_inventory_page(connection.id, run_id, lease_token, page)
            if result == "stale":
                return result
            if result == "more":
                continue
            _persist_refreshed_access_token(connection, client)
            if result == "complete":
                return result
            if not _release_reconciliation_lease(connection.id, run_id, lease_token):
                return "stale"
            _publish_reconciliation_continuation(connection.id, run_id)
            return "continued"
        _persist_refreshed_access_token(connection, client)
        if not _release_reconciliation_lease(connection.id, run_id, lease_token):
            return "stale"
        _publish_reconciliation_continuation(connection.id, run_id)
        return "continued"

    result, correlations = _advance_local_scan(connection.id, run_id, lease_token)
    if result == "stale":
        return result
    for index, correlation in enumerate(correlations):
        _publish_correlation_sync(correlation, index)
    if result == "more":
        if not _persist_local_scan_cursor(connection.id, run_id, lease_token, correlations[-1].id):
            return "stale"
        if not _release_reconciliation_lease(connection.id, run_id, lease_token):
            return "stale"
        _publish_reconciliation_continuation(connection.id, run_id, countdown=len(correlations))
        return "continued"
    return _finish_reconciliation(connection.id, run_id, lease_token)


def _record_present_error(connection, generation, error):
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
        status=GoogleCalendarConnection.Status.PENDING,
    ):
        return "stale"
    mark_google_calendar_connection_error(connection.id, generation, error)
    return "error"


def _record_cleanup_error(connection, generation, error):
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
        status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
    ):
        return "stale"
    record_google_calendar_cleanup_error(connection.id, generation, error)
    return "error"


def _account_has_other_token_bearing_connection(connection):
    if not connection.provider_account_id:
        return False
    return (
        GoogleCalendarConnection.objects.select_for_update()
        .filter(provider_account_id=connection.provider_account_id)
        .exclude(id=connection.id)
        .filter(Q(access_token__gt="") | Q(refresh_token__gt=""))
        .order_by("id")
        .exists()
    )


@transaction.atomic
def _prepare_reconciliation(connection_id, generation):
    """Commit creation identity before any provider operation can use it."""

    try:
        connection = lock_google_calendar_connection(connection_id)
    except GoogleCalendarConnection.DoesNotExist:
        return "missing"
    connection.workspace_integration = WorkspaceIntegration.objects.select_for_update().get(
        id=connection.workspace_integration_id
    )
    if connection.lifecycle_generation != generation:
        return "stale"
    if (
        connection.desired_state == GoogleCalendarConnection.DesiredState.CONNECTED
        and connection.status == GoogleCalendarConnection.Status.PENDING
        and not connection.calendar_id
        and connection.calendar_operation_id is None
    ):
        connection.calendar_operation_id = uuid4()
        connection.save(update_fields=["calendar_operation_id", "updated_at"])
    return connection.desired_state


@transaction.atomic
def _converge_present_transaction(connection, generation):
    connection = lock_google_calendar_connection(connection.id)
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
        status=GoogleCalendarConnection.Status.PENDING,
    ):
        return "stale"
    client = _client_for(connection)
    retained_inventory = bool(connection.calendar_id and connection.calendar_operation_id is None)
    try:
        client.validate_credentials()
        if not has_usable_google_calendar_grant(connection):
            return _record_present_error(connection, generation, "Google Calendar grant is not usable")
        if connection.calendar_operation_id is not None:
            if not _owns_generation(
                connection,
                generation,
                desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
                status=GoogleCalendarConnection.Status.PENDING,
            ):
                return "stale"
            calendar_id = client.find_calendar(connection.calendar_operation_id)
            if calendar_id is None:
                if not _owns_generation(
                    connection,
                    generation,
                    desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
                    status=GoogleCalendarConnection.Status.PENDING,
                ):
                    return "stale"
                calendar_id = client.create_calendar(connection.calendar_operation_id)
            connection.calendar_id = calendar_id
            connection.calendar_generation += 1
            connection.sync_token = ""
            connection.page_token = ""
            connection.reconciliation_phase = ""
            connection.reconciliation_cursor = ""
            connection.reconciliation_lease_expires_at = None
            connection.reconciliation_completed_at = None
            _persist_refreshed_access_token(connection, client)
            if not _owns_generation(
                connection,
                generation,
                desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
                status=GoogleCalendarConnection.Status.PENDING,
            ):
                return "stale"
            connection.save(
                update_fields=[
                    "calendar_id",
                    "calendar_generation",
                    "sync_token",
                    "page_token",
                    "reconciliation_phase",
                    "reconciliation_cursor",
                    "reconciliation_lease_expires_at",
                    "reconciliation_completed_at",
                    "updated_at",
                ]
            )
            GoogleCalendarEvent.objects.filter(connection=connection).update(
                provider_etag="",
                provider_payload_hash="",
                provider_status="",
            )
        elif connection.calendar_id:
            try:
                client.get_calendar(connection.calendar_id)
            except GoogleCalendarCalendarAbsent:
                connection.calendar_operation_id = uuid4()
                connection.save(update_fields=["calendar_operation_id", "updated_at"])
                return "replacement_prepared"
        else:
            return _record_present_error(connection, generation, "Google Calendar creation identity is missing")
    except GoogleCalendarCredentialMismatch:
        return _record_present_error(
            connection,
            generation,
            GoogleCalendarCredentialMismatch.classification,
        )
    except (GoogleCalendarClientError, GoogleCalendarOAuthConfigurationError) as exc:
        _persist_refreshed_access_token(connection, client)
        return _record_present_error(connection, generation, exc)

    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
        status=GoogleCalendarConnection.Status.PENDING,
    ):
        return "stale"
    connection.calendar_operation_id = None
    connection.save(update_fields=["calendar_operation_id", "updated_at"])
    mark_google_calendar_connection_active(connection.id, generation)
    enqueue_google_calendar_task_on_commit(backfill_google_calendar_open_issues, str(connection.id))
    enqueue_google_calendar_task_on_commit(backfill_google_calendar_cycles, str(connection.id))
    if retained_inventory:
        enqueue_google_calendar_task_on_commit(
            reconcile_google_calendar_inventory,
            str(connection.id),
            force_local_scan=True,
        )
    return "active"


def _converge_present(connection, generation):
    result = _converge_present_transaction(connection, generation)
    if result == "replacement_prepared":
        return _converge_present_transaction(connection, generation)
    return result


@transaction.atomic
def _delete_absent_calendar(connection, generation):
    connection = lock_google_calendar_connection(connection.id)
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
        status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
    ):
        return "stale"

    connection.workspace_integration = WorkspaceIntegration.objects.select_for_update().get(
        id=connection.workspace_integration_id
    )
    client = _client_for(connection)
    try:
        client.validate_credentials()
        if not connection.calendar_id and connection.calendar_operation_id is None:
            return "ready"
        if not connection.calendar_id and connection.calendar_operation_id is not None:
            connection.calendar_id = client.find_calendar(connection.calendar_operation_id) or ""
        if connection.calendar_id:
            if not _owns_generation(
                connection,
                generation,
                desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
                status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            ):
                return "stale"
            recorded_calendar_id = connection.calendar_id
            client.delete_calendar(recorded_calendar_id)
            _persist_refreshed_access_token(connection, client)
            if not _owns_generation(
                connection,
                generation,
                desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
                status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
            ):
                return "stale"
            if connection.calendar_id != recorded_calendar_id:
                return "stale"
            connection.calendar_id = ""
        connection.calendar_operation_id = None
        _persist_refreshed_access_token(connection, client)
        connection.save(update_fields=["calendar_id", "calendar_operation_id", "updated_at"])
    except GoogleCalendarCredentialMismatch:
        return _record_cleanup_error(
            connection,
            generation,
            GoogleCalendarCredentialMismatch.classification,
        )
    except (GoogleCalendarClientError, GoogleCalendarOAuthConfigurationError) as exc:
        _persist_refreshed_access_token(connection, client)
        return _record_cleanup_error(connection, generation, exc)
    return "ready"


@transaction.atomic
def _complete_absent(connection, generation):
    connection = lock_google_calendar_connection(connection.id)
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
        status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
    ):
        return "stale"
    if connection.calendar_id or connection.calendar_operation_id is not None:
        return "pending"

    client = _client_for(connection)
    try:
        client.validate_credentials()
    except GoogleCalendarCredentialMismatch:
        return _record_cleanup_error(
            connection,
            generation,
            GoogleCalendarCredentialMismatch.classification,
        )
    except (GoogleCalendarClientError, GoogleCalendarOAuthConfigurationError) as exc:
        return _record_cleanup_error(connection, generation, exc)

    GoogleCalendarEvent.objects.filter(connection=connection).delete(soft=False)

    retain_grant = connection.retain_grant_after_cleanup
    if not retain_grant and not _account_has_other_token_bearing_connection(connection):
        try:
            client.revoke_grant()
        except GoogleCalendarCredentialMismatch:
            return _record_cleanup_error(
                connection,
                generation,
                GoogleCalendarCredentialMismatch.classification,
            )
        except (GoogleCalendarClientError, GoogleCalendarOAuthConfigurationError) as exc:
            return _record_cleanup_error(connection, generation, exc)
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.DISCONNECTED,
        status=GoogleCalendarConnection.Status.CLEANUP_PENDING,
    ):
        return "stale"
    complete_google_calendar_disconnect(connection.id, generation)
    return "disabled" if retain_grant else "disconnected"


@shared_task
def reconcile_google_calendar_connection(connection_id, generation):
    """Converge only one exact durable Calendar lifecycle generation."""

    prepared_state = _prepare_reconciliation(connection_id, generation)
    if prepared_state in {"missing", "stale"}:
        return prepared_state
    try:
        connection = GoogleCalendarConnection.objects.get(id=connection_id)
    except GoogleCalendarConnection.DoesNotExist:
        return "missing"
    if prepared_state == GoogleCalendarConnection.DesiredState.CONNECTED:
        return _converge_present(connection, generation)
    delete_result = _delete_absent_calendar(connection, generation)
    if delete_result != "ready":
        return delete_result
    return _complete_absent(connection, generation)
