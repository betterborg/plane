# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from rest_framework import serializers

from plane.db.models import GoogleCalendarConnection, Issue, Label

from .user import UserLiteSerializer


GOOGLE_CALENDAR_PUBLIC_STATUSES = {
    GoogleCalendarConnection.Status.PENDING: "provisioning",
    GoogleCalendarConnection.Status.ACTIVE: "healthy",
    GoogleCalendarConnection.Status.ERROR: "broken",
    GoogleCalendarConnection.Status.CLEANUP_PENDING: "disconnecting",
}


class GoogleCalendarFilterOptionLabelSerializer(serializers.ModelSerializer):
    """Expose only label metadata needed to configure Calendar filters."""

    class Meta:
        model = Label
        fields = ("id", "name", "color", "project_id")


class GoogleCalendarProjectSyncSerializer(serializers.Serializer):
    """Validate and expose one project's Calendar inclusion setting."""

    google_calendar_sync_enabled = serializers.BooleanField()


class GoogleCalendarWorkspacePolicySerializer(serializers.Serializer):
    """Validate the complete workspace-level Google Calendar policy."""

    enabled = serializers.BooleanField()
    mode = serializers.ChoiceField(choices=("assignment", "filter"), default="assignment")
    update_on_completion = serializers.BooleanField(default=True)
    recipients = serializers.ChoiceField(choices=("cycle_members",), default="cycle_members")
    label_ids = serializers.ListField(
        child=serializers.UUIDField(),
        default=list,
    )
    priorities = serializers.ListField(
        child=serializers.ChoiceField(choices=Issue.PRIORITY_CHOICES),
        default=list,
    )
    label_match = serializers.ChoiceField(choices=("any", "all"), default="any")

    def validate_label_ids(self, value):
        workspace = self.context["workspace"]
        workspace_label_ids = set(Label.objects.filter(id__in=value, workspace=workspace).values_list("id", flat=True))
        if any(label_id not in workspace_label_ids for label_id in value):
            raise serializers.ValidationError("One or more labels do not belong to this workspace")
        return value

    def validate(self, attrs):
        if attrs["mode"] == "filter" and not (attrs["label_ids"] or attrs["priorities"]):
            raise serializers.ValidationError("Filter mode requires a label or priority")
        return attrs


class GoogleCalendarWorkspacePolicyReadSerializer(GoogleCalendarWorkspacePolicySerializer):
    """Serialize only public policy fields, including defaults before first setup."""

    enabled = serializers.BooleanField(default=False)

    def to_representation(self, instance):
        policy = dict(instance)
        if "label_ids" not in policy:
            label_id = policy.get("label_id")
            policy["label_ids"] = [] if label_id is None else [label_id]
        if "priorities" not in policy:
            priority = policy.get("priority")
            policy["priorities"] = [] if priority is None else [priority]
        policy.setdefault("label_match", "any")
        return super().to_representation(policy)


class GoogleCalendarConnectionStatusSerializer(serializers.ModelSerializer):
    """Expose a connection's coarse public state without provider details."""

    status = serializers.SerializerMethodField()

    def get_status(self, obj):
        return GOOGLE_CALENDAR_PUBLIC_STATUSES.get(obj.status)

    class Meta:
        model = GoogleCalendarConnection
        fields = ("status",)


def serialize_google_calendar_connection_status(connection):
    """Serialize public state, treating absent and disconnected rows alike."""
    if connection is None or connection.status not in GOOGLE_CALENDAR_PUBLIC_STATUSES:
        return None
    return GoogleCalendarConnectionStatusSerializer(connection).data


class GoogleCalendarConnectionRosterStatusSerializer(GoogleCalendarConnectionStatusSerializer):
    """Expose roster health, including the most recent successful reconciliation."""

    class Meta(GoogleCalendarConnectionStatusSerializer.Meta):
        fields = ("status", "last_success_at")


class GoogleCalendarConnectionRosterSerializer(serializers.ModelSerializer):
    """Name an active Plane member alongside their public Calendar state."""

    member = UserLiteSerializer(read_only=True)
    connection = GoogleCalendarConnectionRosterStatusSerializer(source="*", read_only=True)

    class Meta:
        model = GoogleCalendarConnection
        fields = ("member", "connection")
