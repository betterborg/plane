# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from plane.app.views import (
    GoogleCalendarConnectionEndpoint,
    GoogleCalendarConnectionRosterEndpoint,
    GoogleCalendarOAuthStartEndpoint,
    GoogleCalendarWorkspacePolicyEndpoint,
    GoogleCalendarWorkspaceStatusEndpoint,
)


urlpatterns = [
    path(
        "workspaces/<str:slug>/integrations/google-calendar/oauth/start/",
        GoogleCalendarOAuthStartEndpoint.as_view(),
        name="google-calendar-oauth-start",
    ),
    path(
        "workspaces/<str:slug>/integrations/google-calendar/policy/",
        GoogleCalendarWorkspacePolicyEndpoint.as_view(),
        name="google-calendar-workspace-policy",
    ),
    path(
        "workspaces/<str:slug>/integrations/google-calendar/status/",
        GoogleCalendarWorkspaceStatusEndpoint.as_view(),
        name="google-calendar-workspace-status",
    ),
    path(
        "workspaces/<str:slug>/integrations/google-calendar/connections/",
        GoogleCalendarConnectionRosterEndpoint.as_view(),
        name="google-calendar-connection-roster",
    ),
    path(
        "workspaces/<str:slug>/integrations/google-calendar/connections/<uuid:member_id>/",
        GoogleCalendarConnectionEndpoint.as_view(),
        name="google-calendar-connection",
    ),
]
