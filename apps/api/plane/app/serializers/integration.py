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


class GoogleCalendarWorkspacePolicySerializer(serializers.Serializer):
    """Validate the complete workspace-level Google Calendar policy."""

    enabled = serializers.BooleanField()
    mode = serializers.ChoiceField(choices=("assignment", "filter"), default="assignment")
    update_on_completion = serializers.BooleanField(default=True)
    recipients = serializers.ChoiceField(choices=("cycle_members",), default="cycle_members")
    label_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    priority = serializers.ChoiceField(
        choices=Issue.PRIORITY_CHOICES,
        required=False,
        allow_null=True,
        default=None,
    )

    def validate_label_id(self, value):
        if value is None:
            return value

        workspace = self.context["workspace"]
        if not Label.objects.filter(id=value, workspace=workspace).exists():
            raise serializers.ValidationError("Label does not belong to this workspace")
        return value

    def validate(self, attrs):
        if attrs["mode"] == "filter" and not (attrs.get("label_id") or attrs.get("priority")):
            raise serializers.ValidationError("Filter mode requires a label or priority")
        return attrs


class GoogleCalendarWorkspacePolicyReadSerializer(GoogleCalendarWorkspacePolicySerializer):
    """Serialize only public policy fields, including defaults before first setup."""

    enabled = serializers.BooleanField(default=False)


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


class GoogleCalendarConnectionRosterSerializer(serializers.ModelSerializer):
    """Name an active Plane member alongside their public Calendar state."""

    member = UserLiteSerializer(read_only=True)
    connection = GoogleCalendarConnectionStatusSerializer(source="*", read_only=True)

    class Meta:
        model = GoogleCalendarConnection
        fields = ("member", "connection")
