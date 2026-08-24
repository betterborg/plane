# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from plane.app.views import (
    GoogleCalendarConnectionEndpoint,
    GoogleCalendarConnectionRosterEndpoint,
    GoogleCalendarFilterOptionsEndpoint,
    GoogleCalendarOAuthStartEndpoint,
    GoogleCalendarProjectSyncEndpoint,
    GoogleCalendarReleaseReadinessEndpoint,
    GoogleCalendarWorkspacePolicyEndpoint,
    GoogleCalendarWorkspaceStatusEndpoint,
)


urlpatterns = [
    path(
        "integrations/google-calendar/readiness/",
        GoogleCalendarReleaseReadinessEndpoint.as_view(),
        name="google-calendar-release-readiness",
    ),
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
        "workspaces/<str:slug>/integrations/google-calendar/filter-options/",
        GoogleCalendarFilterOptionsEndpoint.as_view(),
        name="google-calendar-filter-options",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/integrations/google-calendar/",
        GoogleCalendarProjectSyncEndpoint.as_view(),
        name="google-calendar-project-sync",
    ),
    path(
        "workspaces/<str:slug>/integrations/google-calendar/connections/<uuid:member_id>/",
        GoogleCalendarConnectionEndpoint.as_view(),
        name="google-calendar-connection",
    ),
]
