# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import hmac
import logging

from celery import current_app
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from plane.app.permissions import ProjectAdminPermission, WorkspaceMemberPermission, WorkspaceOwnerPermission
from plane.app.serializers import (
    GoogleCalendarConnectionRosterSerializer,
    GoogleCalendarWorkspacePolicyReadSerializer,
    GoogleCalendarWorkspacePolicySerializer,
)
from plane.app.serializers.integration import (
    GOOGLE_CALENDAR_PUBLIC_STATUSES,
    GoogleCalendarFilterOptionLabelSerializer,
    GoogleCalendarProjectSyncSerializer,
    serialize_google_calendar_connection_status,
)
from plane.app.views.base import BaseAPIView
from plane.db.models import (
    GoogleCalendarConnection,
    Integration,
    Issue,
    Label,
    Project,
    Workspace,
    WorkspaceIntegration,
)
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_LIFECYCLE_TASK,
    enqueue_google_calendar_project_resync_on_commit,
    enqueue_google_calendar_task_on_commit,
    enqueue_google_calendar_workspace_policy_resyncs_on_commit,
)
from plane.integrations.google_calendar.lifecycle import (
    GoogleCalendarDisableCleanupInProgress,
    lock_google_calendar_connection,
    lock_google_calendar_workspace_connections,
    request_google_calendar_disconnect,
    request_google_calendar_workspace_policy_disable,
    request_google_calendar_workspace_policy_enable,
    request_google_calendar_workspace_reconciliation,
)
from plane.integrations.google_calendar.oauth import (
    GoogleCalendarOAuthConfigurationError,
    get_google_calendar_oauth_credentials,
)
from plane.integrations.google_calendar.telemetry import (
    log_google_calendar_operation,
    publish_google_calendar_analytics,
)
from plane.license.api.permissions import InstanceAdminPermission
from plane.license.utils.google_calendar_credentials import google_calendar_credential_fingerprint
from plane.utils.analytics_events import GOOGLE_CALENDAR_ADOPTED


logger = logging.getLogger(__name__)
GOOGLE_CALENDAR_READINESS_OVERDUE_SECONDS = 18 * 60 * 60


def _has_complete_google_calendar_credentials():
    try:
        get_google_calendar_oauth_credentials()
    except GoogleCalendarOAuthConfigurationError:
        return False
    return True


def _google_calendar_release_readiness(at=None):
    """Return aggregate release readiness without exposing provider or credential data."""

    at = at or timezone.now()
    try:
        credentials = get_google_calendar_oauth_credentials()
    except GoogleCalendarOAuthConfigurationError:
        credentials = None

    connections = GoogleCalendarConnection.all_objects.all()
    provider_connections = connections.filter(
        Q(provider_account_id__gt="")
        | Q(provider_email__gt="")
        | Q(calendar_id__gt="")
        | Q(calendar_operation_id__isnull=False)
        | Q(access_token__gt="")
        | Q(refresh_token__gt="")
        | Q(sync_token__gt="")
        | Q(page_token__gt="")
        | Q(token_expires_at__isnull=False)
        | ~Q(scopes=[])
        | Q(credential_fingerprint__gt="")
    )
    binding_fingerprints = list(provider_connections.values_list("credential_fingerprint", flat=True))
    if credentials is None:
        credential_mismatch_count = len(binding_fingerprints)
    else:
        effective_fingerprint = google_calendar_credential_fingerprint(
            credentials.client_id,
            credentials.client_secret,
        )
        credential_mismatch_count = sum(
            not fingerprint or not hmac.compare_digest(fingerprint, effective_fingerprint)
            for fingerprint in binding_fingerprints
        )

    incomplete_lifecycle_count = connections.filter(
        Q(status__in=[GoogleCalendarConnection.Status.PENDING, GoogleCalendarConnection.Status.CLEANUP_PENDING])
        | ~Q(reconciliation_phase="")
    ).count()
    required_verifications = connections.filter(
        workspace_integration__config__enabled=True,
        desired_state=GoogleCalendarConnection.DesiredState.CONNECTED,
        status=GoogleCalendarConnection.Status.ACTIVE,
    )
    required_verification_states = list(
        required_verifications.values_list("calendar_id", "reconciliation_completed_at")
    )
    completed_times = [
        completed_at
        for calendar_id, completed_at in required_verification_states
        if calendar_id and completed_at is not None
    ]
    completion_ages = [max(0, int((at - completed_at).total_seconds())) for completed_at in completed_times]
    overdue_count = sum(
        not calendar_id
        or completed_at is None
        or (at - completed_at).total_seconds() >= GOOGLE_CALENDAR_READINESS_OVERDUE_SECONDS
        for calendar_id, completed_at in required_verification_states
    )

    configuration_complete = credentials is not None
    credential_binding_complete = configuration_complete and credential_mismatch_count == 0
    lifecycle_recovery_complete = incomplete_lifecycle_count == 0
    backend_verification_complete = bool(
        lifecycle_recovery_complete and len(completed_times) == len(required_verification_states) and overdue_count == 0
    )
    ready = configuration_complete and credential_binding_complete and backend_verification_complete
    return {
        "ready": ready,
        "released": settings.GOOGLE_CALENDAR_RELEASED,
        "configuration_complete": configuration_complete,
        "credential_binding_complete": credential_binding_complete,
        "lifecycle_recovery_complete": lifecycle_recovery_complete,
        "backend_verification_complete": backend_verification_complete,
        "reconciliation_overdue": overdue_count > 0,
        "provider_connection_count": len(binding_fingerprints),
        "credential_mismatch_count": credential_mismatch_count,
        "incomplete_lifecycle_count": incomplete_lifecycle_count,
        "required_verification_count": len(required_verification_states),
        "completed_verification_count": len(completed_times),
        "overdue_reconciliation_count": overdue_count,
        "last_completion_age_seconds": max(completion_ages, default=None),
    }


def _calendar_workspace_integration(workspace):
    return (
        WorkspaceIntegration.objects.filter(
            workspace=workspace,
            integration__provider="google_calendar",
        )
        .select_related("integration")
        .first()
    )


class GoogleCalendarWorkspaceStatusEndpoint(BaseAPIView):
    """Return Calendar availability, public policy, and the caller's status."""

    permission_classes = [WorkspaceMemberPermission]

    def get_permissions(self):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return [IsAuthenticated()]
        return super().get_permissions()

    def get(self, request, slug):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        workspace = Workspace.objects.get(slug=slug)
        workspace_integration = _calendar_workspace_integration(workspace)
        policy = GoogleCalendarWorkspacePolicyReadSerializer(
            workspace_integration.config if workspace_integration is not None else {}
        ).data
        connection = None
        if workspace_integration is not None:
            connection = GoogleCalendarConnection.objects.filter(
                workspace_integration=workspace_integration,
                member=request.user,
            ).first()

        return Response(
            {
                "available": _has_complete_google_calendar_credentials(),
                "policy": policy,
                "connection": serialize_google_calendar_connection_status(connection),
            },
            status=status.HTTP_200_OK,
        )


class GoogleCalendarReleaseReadinessEndpoint(BaseAPIView):
    """Return secret-free Calendar backend readiness to instance administrators."""

    permission_classes = [InstanceAdminPermission]

    def get(self, request):
        readiness = _google_calendar_release_readiness()
        if not readiness["credential_binding_complete"]:
            action = "credential_binding"
        elif not readiness["lifecycle_recovery_complete"]:
            action = "lifecycle_recovery"
        elif not readiness["backend_verification_complete"]:
            action = "backend_verification"
        else:
            action = "ready"
        log_google_calendar_operation(
            logger,
            "release_readiness",
            outcome="ready" if readiness["ready"] else "not_ready",
            local_candidate_count=readiness["required_verification_count"],
            reconciliation_action=action,
            last_completion_age_seconds=readiness["last_completion_age_seconds"],
        )
        return Response(readiness, status=status.HTTP_200_OK)


class GoogleCalendarConnectionRosterEndpoint(BaseAPIView):
    """Return active members with a non-tombstone Calendar connection."""

    permission_classes = [WorkspaceOwnerPermission]

    def get_permissions(self):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return [IsAuthenticated()]
        return super().get_permissions()

    def get(self, request, slug):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        connections = (
            GoogleCalendarConnection.objects.filter(
                workspace_integration__workspace__slug=slug,
                workspace_integration__integration__provider="google_calendar",
                member__member_workspace__workspace__slug=slug,
                member__member_workspace__is_active=True,
                member__member_workspace__deleted_at__isnull=True,
                status__in=GOOGLE_CALENDAR_PUBLIC_STATUSES,
            )
            .select_related("member")
            .order_by("member__display_name", "member_id")
        )
        return Response(
            GoogleCalendarConnectionRosterSerializer(connections, many=True).data,
            status=status.HTTP_200_OK,
        )


class GoogleCalendarFilterOptionsEndpoint(BaseAPIView):
    """Return workspace label and priority choices for Calendar filters."""

    permission_classes = [WorkspaceOwnerPermission]

    def get_permissions(self):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return [IsAuthenticated()]
        return super().get_permissions()

    def get(self, request, slug):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        labels = Label.objects.filter(
            workspace__slug=slug,
            project__archived_at__isnull=True,
        ).order_by("name", "id")
        priorities = [{"key": key, "title": title} for key, title in Issue.PRIORITY_CHOICES]
        return Response(
            {
                "labels": GoogleCalendarFilterOptionLabelSerializer(labels, many=True).data,
                "priorities": priorities,
            },
            status=status.HTTP_200_OK,
        )


class GoogleCalendarProjectSyncEndpoint(BaseAPIView):
    """Read or mutate Calendar inclusion for one project."""

    permission_classes = [ProjectAdminPermission]

    def get_permissions(self):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return [IsAuthenticated()]
        return super().get_permissions()

    def get(self, request, slug, project_id):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        project = Project.objects.get(pk=project_id, workspace__slug=slug)
        return Response(GoogleCalendarProjectSyncSerializer(project).data, status=status.HTTP_200_OK)

    @transaction.atomic
    def patch(self, request, slug, project_id):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        project = Project.objects.get(pk=project_id, workspace__slug=slug)
        serializer = GoogleCalendarProjectSyncSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project.google_calendar_sync_enabled = serializer.validated_data["google_calendar_sync_enabled"]
        project.save(update_fields=["google_calendar_sync_enabled", "updated_at"])
        enqueue_google_calendar_project_resync_on_commit(project.id)
        return Response(GoogleCalendarProjectSyncSerializer(project).data, status=status.HTTP_200_OK)


class GoogleCalendarConnectionEndpoint(BaseAPIView):
    """Disconnect only the requesting member's Calendar connection."""

    permission_classes = [WorkspaceMemberPermission]

    def get_permissions(self):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return [IsAuthenticated()]
        return super().get_permissions()

    @transaction.atomic
    def delete(self, request, slug, member_id):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        if member_id != request.user.id:
            return Response({"error": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        connection = (
            GoogleCalendarConnection.objects.filter(
                workspace_integration__workspace__slug=slug,
                workspace_integration__integration__provider="google_calendar",
                member_id=member_id,
            )
            .order_by("id")
            .first()
        )
        if connection is None:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        connection = lock_google_calendar_connection(connection.id)
        command = request_google_calendar_disconnect(connection.id, connection.lifecycle_generation)
        if command is None:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        lifecycle_task = current_app.signature(GOOGLE_CALENDAR_LIFECYCLE_TASK)
        enqueue_google_calendar_task_on_commit(
            lifecycle_task,
            str(command.connection_id),
            command.generation,
        )
        return Response({"status": "disconnecting"}, status=status.HTTP_202_ACCEPTED)


class GoogleCalendarWorkspacePolicyEndpoint(BaseAPIView):
    """Mutate the complete Google Calendar policy for one workspace."""

    permission_classes = [WorkspaceOwnerPermission]

    def get_permissions(self):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return [IsAuthenticated()]
        return super().get_permissions()

    @transaction.atomic
    def patch(self, request, slug):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        workspace = Workspace.objects.get(slug=slug)
        serializer = GoogleCalendarWorkspacePolicySerializer(data=request.data, context={"workspace": workspace})
        serializer.is_valid(raise_exception=True)
        policy = dict(serializer.validated_data)
        serialized_policy = dict(serializer.data)
        if policy["enabled"] and not _has_complete_google_calendar_credentials():
            return Response(
                {"error": "google_calendar_credentials_incomplete"},
                status=status.HTTP_409_CONFLICT,
            )

        integration = Integration.objects.filter(provider="google_calendar").first()
        if integration is None:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        workspace_integration, _ = WorkspaceIntegration.objects.get_or_create(
            workspace=workspace,
            integration=integration,
            defaults={"config": {}},
        )
        lock_google_calendar_workspace_connections(workspace.id)
        workspace_integration = WorkspaceIntegration.objects.select_for_update().get(id=workspace_integration.id)
        previous_policy = dict(workspace_integration.config or {})
        was_enabled = bool(workspace_integration.config.get("enabled", False))
        try:
            if policy["enabled"] and not was_enabled:
                commands = request_google_calendar_workspace_policy_enable(workspace_integration.workspace_id)
            elif policy["enabled"]:
                commands = request_google_calendar_workspace_reconciliation(workspace_integration.workspace_id)
            elif was_enabled:
                commands = request_google_calendar_workspace_policy_disable(workspace_integration.workspace_id)
            else:
                commands = []
        except GoogleCalendarDisableCleanupInProgress:
            return Response(
                {"error": "google_calendar_disable_cleanup_in_progress"},
                status=status.HTTP_409_CONFLICT,
            )

        workspace_integration.config = serialized_policy
        workspace_integration.save(update_fields=["config", "updated_at"])
        for command in commands:
            lifecycle_task = current_app.signature(GOOGLE_CALENDAR_LIFECYCLE_TASK)
            enqueue_google_calendar_task_on_commit(
                lifecycle_task,
                str(command.connection_id),
                command.generation,
            )
        enqueue_google_calendar_workspace_policy_resyncs_on_commit(
            workspace_integration,
            previous_policy,
            serialized_policy,
        )
        if policy["enabled"] and not was_enabled:

            def _publish_adoption_telemetry():
                log_google_calendar_operation(
                    logger,
                    "workspace_adoption",
                    workspace_id=workspace.id,
                    outcome="enabled",
                    reconciliation_action="adopt",
                )
                publish_google_calendar_analytics(
                    GOOGLE_CALENDAR_ADOPTED,
                    user_id=request.user.id,
                    workspace_id=workspace.id,
                    workspace_slug=workspace.slug,
                    outcome="enabled",
                    reconciliation_action="adopt",
                )

            transaction.on_commit(
                _publish_adoption_telemetry,
                robust=True,
            )
        return Response(serialized_policy, status=status.HTTP_200_OK)
