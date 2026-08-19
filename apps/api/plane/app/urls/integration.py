# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from plane.app.views import GoogleCalendarWorkspacePolicyEndpoint


urlpatterns = [
    path(
        "workspaces/<str:slug>/integrations/google-calendar/policy/",
        GoogleCalendarWorkspacePolicyEndpoint.as_view(),
        name="google-calendar-workspace-policy",
    ),
]
