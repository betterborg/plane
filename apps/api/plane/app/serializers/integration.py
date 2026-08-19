# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from rest_framework import serializers

from plane.db.models import Issue, Label


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
