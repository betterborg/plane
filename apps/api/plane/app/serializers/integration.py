# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from rest_framework import serializers


class GoogleCalendarWorkspacePolicySerializer(serializers.Serializer):
    """Validate the complete workspace-level Google Calendar policy."""

    enabled = serializers.BooleanField()
    mode = serializers.ChoiceField(choices=("assignment", "filter"), default="assignment")
    update_on_completion = serializers.BooleanField(default=True)
    recipients = serializers.CharField(default="cycle_members", allow_blank=False)
    label_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    priority = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)

    def validate(self, attrs):
        if attrs["mode"] == "filter" and not (attrs.get("label_id") or attrs.get("priority")):
            raise serializers.ValidationError("Filter mode requires a label or priority")
        return attrs
