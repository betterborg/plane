# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

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
    GoogleCalendarClient,
    GoogleCalendarClientConflict,
    GoogleCalendarClientError,
    GoogleCalendarCredentialMismatch,
)
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_CYCLE_BACKFILL_TASK,
    GOOGLE_CALENDAR_CYCLE_SYNC_TASK,
    GOOGLE_CALENDAR_ISSUE_SYNC_TASK,
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
    if connection.calendar_id:
        client = _client_for(connection)
        try:
            client.delete_event(connection.calendar_id, correlation.google_event_id)
        except GoogleCalendarCredentialMismatch:
            _mark_credential_mismatch(connection)
            return False
        _persist_refreshed_access_token(connection, client)
    correlation.delete(soft=False)
    return True


def _converge_provider_event(connection, entity_type, entity_id, payload, correlation):
    payload_hash = google_calendar_payload_hash(payload)
    if correlation is not None and correlation.payload_hash == payload_hash:
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
    except GoogleCalendarCredentialMismatch:
        _mark_credential_mismatch(connection)
        return "credential_mismatch"


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
    if correlation is None:
        provider_event_id = deterministic_event_id
        try:
            client.insert_event(connection.calendar_id, provider_event_id, payload)
        except GoogleCalendarClientConflict:
            provider_event_id = _client_event_id_from_recovery(
                client,
                connection,
                entity_id,
                deterministic_event_id,
            )
            if provider_event_id is None:
                client.insert_event(connection.calendar_id, deterministic_event_id, payload)
                provider_event_id = deterministic_event_id
            else:
                client.update_event(connection.calendar_id, provider_event_id, payload)
        GoogleCalendarEvent.objects.create(
            connection=connection,
            entity_type=entity_type,
            entity_id=entity_id,
            google_event_id=provider_event_id,
            payload_hash=payload_hash,
            calendar_generation=connection.calendar_generation,
            last_synced_at=timezone.now(),
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
        if not _delete_provider_event(connection, correlation):
            return "credential_mismatch"
        return "deleted"

    eligible = is_issue_assignment_eligible(issue, connection)
    update_terminal = bool(connection.workspace_integration.config.get("update_on_completion", True))
    terminal = issue.state and issue.state.group in {StateGroup.COMPLETED, StateGroup.CANCELLED}
    if not eligible or (terminal and not update_terminal):
        if correlation is None:
            return "ineligible"
        if not _delete_provider_event(connection, correlation):
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


@transaction.atomic
def _synchronize_cycle_for_connection(cycle_id, connection_id):
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
        if not _delete_provider_event(connection, correlation):
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
        if not _delete_provider_event(connection, correlation):
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


@shared_task(
    autoretry_for=(GoogleCalendarClientError, GoogleCalendarOAuthConfigurationError),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5},
    rate_limit="120/m",
)
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
    from plane.db.signals import dispatch_google_calendar_issue_sync

    for issue_id in current_batch:
        dispatch_google_calendar_issue_sync(issue_id)

    if len(issue_ids) > len(current_batch) and current_batch:
        continuation = current_app.signature(GOOGLE_CALENDAR_STATE_ISSUE_RESYNC_TASK)
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
    from plane.db.signals import dispatch_google_calendar_issue_sync

    for issue_id in current_batch:
        dispatch_google_calendar_issue_sync(issue_id)

    if len(issue_ids) > len(current_batch) and current_batch:
        continuation = current_app.signature(GOOGLE_CALENDAR_LABEL_ISSUE_RESYNC_TASK)
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

    for index, issue_id in enumerate(current_batch):
        sync_task = current_app.signature(GOOGLE_CALENDAR_ISSUE_SYNC_TASK).set(countdown=index)
        publish_google_calendar_task(sync_task, str(issue_id))

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

    for index, cycle_id in enumerate(current_batch):
        sync_task = current_app.signature(GOOGLE_CALENDAR_CYCLE_SYNC_TASK).set(countdown=index)
        publish_google_calendar_task(sync_task, str(cycle_id))

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
        for index, issue_id in enumerate(current_batch):
            sync_task = current_app.signature(GOOGLE_CALENDAR_ISSUE_SYNC_TASK).set(countdown=index)
            publish_google_calendar_task(sync_task, str(issue_id))

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
        for index, cycle_id in enumerate(current_batch):
            sync_task = current_app.signature(GOOGLE_CALENDAR_CYCLE_SYNC_TASK).set(countdown=index)
            publish_google_calendar_task(sync_task, str(cycle_id))

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
    client = _client_for(connection)
    try:
        client.validate_credentials()
        if not has_usable_google_calendar_grant(connection):
            return _record_present_error(connection, generation, "Google Calendar grant is not usable")
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
            connection.calendar_generation += 1
            _persist_refreshed_access_token(connection, client)
            if not _owns_generation(
                connection,
                generation,
                desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
                status=GoogleCalendarConnection.Status.PENDING,
            ):
                return "stale"
            connection.save(update_fields=["calendar_id", "calendar_generation", "updated_at"])
        else:
            client.get_calendar(connection.calendar_id)
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

    GoogleCalendarEvent.objects.filter(connection=connection).delete(soft=False)

    retain_grant = connection.retain_grant_after_cleanup
    if not retain_grant and not _account_has_other_token_bearing_connection(connection):
        client = _client_for(connection)
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
