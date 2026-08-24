# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from celery import current_app
from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver

from plane.db.models import GoogleCalendarConnection, WorkspaceMember
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_LIFECYCLE_TASK,
    enqueue_google_calendar_task_on_commit,
)
from plane.integrations.google_calendar.lifecycle import request_google_calendar_disconnect


_WAS_ACTIVE_MEMBERSHIP_ATTRIBUTE = "_google_calendar_was_active_membership"


def _request_removed_member_cleanup(workspace_id, member_id):
    calendar_connection = (
        GoogleCalendarConnection.objects.filter(
            workspace_integration__workspace_id=workspace_id,
            workspace_integration__workspace__deleted_at__isnull=True,
            workspace_integration__integration__provider="google_calendar",
            member_id=member_id,
        )
        .order_by("id")
        .first()
    )
    if calendar_connection is None:
        return

    command = request_google_calendar_disconnect(
        calendar_connection.id,
        calendar_connection.lifecycle_generation,
    )
    if command is None:
        return

    lifecycle_task = current_app.signature(GOOGLE_CALENDAR_LIFECYCLE_TASK)
    enqueue_google_calendar_task_on_commit(
        lifecycle_task,
        str(command.connection_id),
        command.generation,
    )


@receiver(
    pre_save,
    sender=WorkspaceMember,
    dispatch_uid="google_calendar_workspace_member_pre_save",
)
def capture_google_calendar_workspace_member_state(sender, instance, **kwargs):
    """Remember whether an existing membership was active before this save."""

    was_active = False
    if not instance._state.adding:
        previous_state = WorkspaceMember.all_objects.filter(id=instance.id).values("is_active", "deleted_at").first()
        was_active = bool(previous_state and previous_state["is_active"] and previous_state["deleted_at"] is None)
    setattr(instance, _WAS_ACTIVE_MEMBERSHIP_ATTRIBUTE, was_active)


@receiver(
    post_save,
    sender=WorkspaceMember,
    dispatch_uid="google_calendar_workspace_member_post_save",
)
def cleanup_deactivated_google_calendar_workspace_member(sender, instance, **kwargs):
    """Disconnect Calendar when a live membership is deactivated or soft-deleted."""

    was_active = getattr(instance, _WAS_ACTIVE_MEMBERSHIP_ATTRIBUTE, False)
    is_removed = not instance.is_active or instance.deleted_at is not None
    if was_active and is_removed:
        _request_removed_member_cleanup(instance.workspace_id, instance.member_id)


@receiver(
    pre_delete,
    sender=WorkspaceMember,
    dispatch_uid="google_calendar_workspace_member_pre_delete",
)
def cleanup_deleted_google_calendar_workspace_member(sender, instance, **kwargs):
    """Disconnect Calendar before an active membership is hard-deleted."""

    if instance.is_active and instance.deleted_at is None:
        _request_removed_member_cleanup(instance.workspace_id, instance.member_id)
