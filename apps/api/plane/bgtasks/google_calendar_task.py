# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import UUID, uuid4

from celery import current_app, shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from plane.db.models import GoogleCalendarConnection, GoogleCalendarEvent, Issue, IssueAssignee, WorkspaceIntegration
from plane.db.models.state import StateGroup
from plane.integrations.google_calendar.client import (
    GoogleCalendarClient,
    GoogleCalendarClientConflict,
    GoogleCalendarClientError,
)
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_ISSUE_SYNC_TASK,
    GOOGLE_CALENDAR_OPEN_BACKFILL_TASK,
    GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK,
    enqueue_google_calendar_task_on_commit,
)
from plane.integrations.google_calendar.eligibility import (
    is_issue_assignment_eligible,
    is_issue_open_backfill_eligible,
)
from plane.integrations.google_calendar.events import (
    build_google_calendar_work_item_event,
    google_calendar_event_id,
    google_calendar_payload_hash,
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


def _owns_generation(connection, generation, *, desired_state, status):
    return (
        connection.lifecycle_generation == generation
        and connection.desired_state == desired_state
        and connection.status == status
    )


def _client_for(connection):
    return GoogleCalendarClient(
        access_token=connection.access_token,
        refresh_token=connection.refresh_token,
        token_expires_at=connection.token_expires_at,
    )


def _persist_refreshed_access_token(connection, client):
    access_token = client.access_token
    if access_token is None:
        return
    if connection.access_token == access_token.value and connection.token_expires_at == access_token.expires_at:
        return
    connection.access_token = access_token.value
    connection.token_expires_at = access_token.expires_at
    connection.save(update_fields=["access_token", "token_expires_at", "updated_at"])


def _issue_queryset():
    return Issue.all_objects.select_related("workspace", "project", "state")


def _client_event_id_from_recovery(client, connection, issue, deterministic_event_id):
    existing_event = client.get_event(connection.calendar_id, deterministic_event_id)
    if existing_event is not None:
        return deterministic_event_id

    marker = f"plane_entity_id={issue.id}"
    recovered_events = client.list_events(
        connection.calendar_id,
        private_extended_property=marker,
    )
    for event in recovered_events:
        recovered_id = event.get("id")
        if isinstance(recovered_id, str) and recovered_id:
            return recovered_id
    return None


def _delete_work_item_event(connection, correlation):
    if connection.calendar_id:
        client = _client_for(connection)
        client.delete_event(connection.calendar_id, correlation.google_event_id)
        _persist_refreshed_access_token(connection, client)
    correlation.delete(soft=False)


@transaction.atomic
def _synchronize_issue_for_connection(issue_id, connection_id):
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
        _delete_work_item_event(connection, correlation)
        return "deleted"

    eligible = is_issue_assignment_eligible(issue, connection)
    update_terminal = bool(connection.workspace_integration.config.get("update_on_completion", True))
    terminal = issue.state and issue.state.group in {StateGroup.COMPLETED, StateGroup.CANCELLED}
    if not eligible or (terminal and not update_terminal):
        if correlation is None:
            return "ineligible"
        _delete_work_item_event(connection, correlation)
        return "deleted"

    payload = build_google_calendar_work_item_event(issue)
    payload_hash = google_calendar_payload_hash(payload)
    if correlation is not None and correlation.payload_hash == payload_hash:
        return "unchanged"

    client = _client_for(connection)
    deterministic_event_id = google_calendar_event_id(
        connection.id,
        GoogleCalendarEvent.EntityType.WORK_ITEM,
        issue.id,
    )
    if correlation is None:
        provider_event_id = deterministic_event_id
        try:
            client.insert_event(connection.calendar_id, provider_event_id, payload)
        except GoogleCalendarClientConflict:
            provider_event_id = _client_event_id_from_recovery(
                client,
                connection,
                issue,
                deterministic_event_id,
            )
            if provider_event_id is None:
                client.insert_event(connection.calendar_id, deterministic_event_id, payload)
                provider_event_id = deterministic_event_id
            else:
                client.update_event(connection.calendar_id, provider_event_id, payload)
        correlation = GoogleCalendarEvent.objects.create(
            connection=connection,
            entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
            entity_id=issue.id,
            google_event_id=provider_event_id,
            payload_hash=payload_hash,
            last_synced_at=timezone.now(),
        )
        _persist_refreshed_access_token(connection, client)
        return "created"

    provider_event_id = _client_event_id_from_recovery(
        client,
        connection,
        issue,
        correlation.google_event_id,
    )
    if provider_event_id is None:
        try:
            client.insert_event(connection.calendar_id, deterministic_event_id, payload)
            provider_event_id = deterministic_event_id
        except GoogleCalendarClientConflict:
            provider_event_id = deterministic_event_id
            client.update_event(connection.calendar_id, provider_event_id, payload)
    else:
        client.update_event(connection.calendar_id, provider_event_id, payload)
    correlation.google_event_id = provider_event_id
    correlation.payload_hash = payload_hash
    correlation.last_synced_at = timezone.now()
    correlation.save(update_fields=["google_event_id", "payload_hash", "last_synced_at", "updated_at"])
    _persist_refreshed_access_token(connection, client)
    return "updated"


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


@shared_task(
    autoretry_for=(GoogleCalendarClientError, GoogleCalendarOAuthConfigurationError),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5},
    rate_limit="120/m",
)
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


@shared_task
def backfill_google_calendar_open_issues(connection_id, after_id=None, batch_size=100):
    """Publish a bounded, paced batch of current open work items for one connection."""

    try:
        connection = GoogleCalendarConnection.objects.select_related("workspace_integration").get(id=connection_id)
    except GoogleCalendarConnection.DoesNotExist:
        return "missing"

    queryset = (
        _issue_queryset()
        .filter(
            workspace_id=connection.workspace_integration.workspace_id,
            target_date__isnull=False,
            issue_assignee__assignee_id=connection.member_id,
        )
        .distinct()
        .order_by("id")
    )
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


@shared_task(bind=True, max_retries=12)
def resync_google_calendar_workspace_issues(task, workspace_id, after_id=None, batch_size=100):
    """Publish a bounded, paced convergence pass for every workspace work item."""

    if GoogleCalendarConnection.objects.filter(
        workspace_integration__workspace_id=workspace_id,
        desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
        status=GoogleCalendarConnection.Status.PENDING,
    ).exists():
        raise task.retry(countdown=5)

    issue_ids = list(_workspace_issue_ids_for_resync(workspace_id, after_id)[: int(batch_size) + 1])
    current_batch = issue_ids[: int(batch_size)]

    for index, issue_id in enumerate(current_batch):
        sync_task = current_app.signature(GOOGLE_CALENDAR_ISSUE_SYNC_TASK).set(countdown=index)
        enqueue_google_calendar_task_on_commit(sync_task, str(issue_id))

    if len(issue_ids) > len(current_batch) and current_batch:
        next_batch = current_app.signature(GOOGLE_CALENDAR_WORKSPACE_ISSUE_RESYNC_TASK).set(
            countdown=len(current_batch)
        )
        enqueue_google_calendar_task_on_commit(
            next_batch,
            str(workspace_id),
            str(current_batch[-1]),
            int(batch_size),
        )
    return len(current_batch)


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
def _converge_present(connection, generation):
    connection = lock_google_calendar_connection(connection.id)
    if not _owns_generation(
        connection,
        generation,
        desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
        status=GoogleCalendarConnection.Status.PENDING,
    ):
        return "stale"
    if not has_usable_google_calendar_grant(connection):
        return _record_present_error(connection, generation, "Google Calendar grant is not usable")

    client = _client_for(connection)
    try:
        if not connection.calendar_id:
            if not _owns_generation(
                connection,
                generation,
                desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
                status=GoogleCalendarConnection.Status.PENDING,
            ):
                return "stale"
            if connection.calendar_operation_id is None:
                return _record_present_error(connection, generation, "Google Calendar creation identity is missing")
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
            _persist_refreshed_access_token(connection, client)
            if not _owns_generation(
                connection,
                generation,
                desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
                status=GoogleCalendarConnection.Status.PENDING,
            ):
                return "stale"
            connection.save(update_fields=["calendar_id", "updated_at"])
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
    return "active"


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
    if not connection.calendar_id and connection.calendar_operation_id is None:
        return "ready"

    client = _client_for(connection)
    try:
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

    GoogleCalendarEvent.objects.filter(connection=connection).delete(soft=False)

    retain_grant = connection.retain_grant_after_cleanup
    if not retain_grant and not _account_has_other_token_bearing_connection(connection):
        client = _client_for(connection)
        try:
            client.revoke_grant()
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
