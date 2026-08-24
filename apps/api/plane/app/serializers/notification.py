# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Module imports
from .base import BaseSerializer
from .user import UserLiteSerializer
from plane.db.models import Notification, UserNotificationPreference

# Third Party imports
from rest_framework import serializers


class NotificationSerializer(BaseSerializer):
    triggered_by_details = UserLiteSerializer(read_only=True, source="triggered_by")
    is_inbox_issue = serializers.BooleanField(read_only=True)
    is_intake_issue = serializers.BooleanField(read_only=True)
    is_mentioned_notification = serializers.BooleanField(read_only=True)

    class Meta:
        model = Notification
        fields = "__all__"

    def to_representation(self, instance):
        response = super().to_representation(instance)
        if instance.entity_name != "issue":
            response.pop("is_inbox_issue", None)
            response.pop("is_intake_issue", None)
            response.pop("is_mentioned_notification", None)
        return response


class UserNotificationPreferenceSerializer(BaseSerializer):
    class Meta:
        model = UserNotificationPreference
        fields = "__all__"
