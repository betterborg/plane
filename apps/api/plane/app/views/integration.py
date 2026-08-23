# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from celery import current_app
from django.conf import settings
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from plane.app.permissions import WorkspaceMemberPermission, WorkspaceOwnerPermission
from plane.app.serializers import (
    GoogleCalendarConnectionRosterSerializer,
    GoogleCalendarWorkspacePolicyReadSerializer,
    GoogleCalendarWorkspacePolicySerializer,
)
from plane.app.serializers.integration import (
    GOOGLE_CALENDAR_PUBLIC_STATUSES,
    serialize_google_calendar_connection_status,
)
from plane.app.views.base import BaseAPIView
from plane.db.models import GoogleCalendarConnection, Integration, Workspace, WorkspaceIntegration
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_LIFECYCLE_TASK,
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


def _has_complete_google_calendar_credentials():
    try:
        get_google_calendar_oauth_credentials()
    except GoogleCalendarOAuthConfigurationError:
        return False
    return True


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

        workspace_integration.config = serializer.data
        workspace_integration.save(update_fields=["config", "updated_at"])
        for command in commands:
            lifecycle_task = current_app.signature(GOOGLE_CALENDAR_LIFECYCLE_TASK)
            enqueue_google_calendar_task_on_commit(
                lifecycle_task,
                str(command.connection_id),
                command.generation,
            )
        enqueue_google_calendar_workspace_policy_resyncs_on_commit(
            workspace_integration.workspace_id,
            previous_policy,
            policy,
        )
        return Response(serializer.data, status=status.HTTP_200_OK)
