# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party imports
from rest_framework import serializers

# Module imports
from .base import BaseSerializer
from .issue import IssueStateSerializer
from plane.db.models import Cycle, CycleIssue, CycleUserProperties, Project
from plane.utils.timezone_converter import normalize_cycle_date_fields


class CycleWriteSerializer(BaseSerializer):
    def validate(self, data):
        project_id = (
            self.context.get("project_id", None)
            or (self.instance and self.instance.project_id)
            or self.initial_data.get("project_id", None)
        )
        date_fields = ("start_date", "end_date")
        dates_changed = any(field in data for field in date_fields)

        if self.instance is None or dates_changed:
            project = Project.objects.only("timezone").get(id=project_id)
            normalize_cycle_date_fields(
                data=data,
                instance=self.instance,
                project_id=project_id,
                project_timezone=project.timezone,
            )
            data["timezone"] = project.timezone

        start_date = data.get("start_date", self.instance.start_date if self.instance else None)
        end_date = data.get("end_date", self.instance.end_date if self.instance else None)
        if start_date is not None and end_date is not None and start_date > end_date:
            raise serializers.ValidationError("Start date cannot exceed end date")

        return data

    class Meta:
        model = Cycle
        fields = "__all__"
        read_only_fields = ["workspace", "project", "owned_by", "archived_at", "timezone"]


class CycleSerializer(BaseSerializer):
    # favorite
    is_favorite = serializers.BooleanField(read_only=True)
    total_issues = serializers.IntegerField(read_only=True)
    # state group wise distribution
    cancelled_issues = serializers.IntegerField(read_only=True)
    completed_issues = serializers.IntegerField(read_only=True)
    started_issues = serializers.IntegerField(read_only=True)
    unstarted_issues = serializers.IntegerField(read_only=True)
    backlog_issues = serializers.IntegerField(read_only=True)

    # active | draft | upcoming | completed
    status = serializers.CharField(read_only=True)

    class Meta:
        model = Cycle
        fields = [
            # necessary fields
            "id",
            "workspace_id",
            "project_id",
            # model fields
            "name",
            "description",
            "start_date",
            "end_date",
            "timezone",
            "owned_by_id",
            "view_props",
            "sort_order",
            "external_source",
            "external_id",
            "progress_snapshot",
            "logo_props",
            # meta fields
            "is_favorite",
            "total_issues",
            "cancelled_issues",
            "completed_issues",
            "started_issues",
            "unstarted_issues",
            "backlog_issues",
            "status",
        ]
        read_only_fields = fields


class CycleIssueSerializer(BaseSerializer):
    issue_detail = IssueStateSerializer(read_only=True, source="issue")
    sub_issues_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = CycleIssue
        fields = "__all__"
        read_only_fields = ["workspace", "project", "cycle"]


class CycleUserPropertiesSerializer(BaseSerializer):
    class Meta:
        model = CycleUserProperties
        fields = "__all__"
        read_only_fields = ["workspace", "project", "cycle", "user"]
