# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import secrets
from datetime import timedelta
from urllib.parse import urlencode

from celery import current_app
from django.conf import settings
from django.core import signing
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from plane.app.permissions import WorkspaceMemberPermission
from plane.app.views.base import BaseAPIView
from plane.db.models import GoogleCalendarConnection, Integration, WorkspaceIntegration, WorkspaceMember
from plane.integrations.google_calendar.dispatch import (
    GOOGLE_CALENDAR_LIFECYCLE_TASK,
    enqueue_google_calendar_task_on_commit,
)
from plane.integrations.google_calendar.lifecycle import (
    GoogleCalendarLifecycleError,
    apply_google_calendar_oauth_success,
    clear_google_calendar_oauth_attempt,
    lock_google_calendar_connection,
    lock_google_calendar_workspace_connections,
)
from plane.integrations.google_calendar.oauth import (
    GoogleCalendarOAuthConfigurationError,
    GoogleCalendarOAuthError,
    GoogleCalendarOAuthIdentityError,
    GoogleCalendarOAuthScopeError,
    build_google_calendar_consent_url,
    create_google_calendar_pkce_pair,
    exchange_google_calendar_code,
    get_google_calendar_identity,
    get_google_calendar_oauth_credentials,
    google_calendar_account_has_live_grant,
    revoke_rejected_google_calendar_grant,
    validate_google_calendar_grant_scopes,
)
from plane.utils.host import base_host


GOOGLE_CALENDAR_OAUTH_SESSION_KEY = "google_calendar_oauth_attempt"
GOOGLE_CALENDAR_OAUTH_STATE_SALT = "plane.google-calendar.oauth"
GOOGLE_CALENDAR_OAUTH_ATTEMPT_SECONDS = 10 * 60
GOOGLE_CALENDAR_OAUTH_MAX_STATE_LENGTH = 1024


class StaleGoogleCalendarOAuthAttempt(GoogleCalendarOAuthError):
    """Raised when a callback no longer owns its attempt and lifecycle generation."""


def _clear_and_save_oauth_attempt(connection):
    clear_google_calendar_oauth_attempt(connection)
    connection.save(
        update_fields=[
            "oauth_state",
            "oauth_code_verifier",
            "oauth_redirect_uri",
            "oauth_attempt_expires_at",
            "updated_at",
        ]
    )


def _calendar_redirect(request, *, workspace_slug="", error=""):
    base_url = base_host(request=request, is_app=True).rstrip("/")
    path = f"/{workspace_slug}/settings/integrations/" if workspace_slug else "/"
    params = {"error": error} if error else {"google_calendar_oauth": "success"}
    return HttpResponseRedirect(f"{base_url}{path}?{urlencode(params)}")


def _decode_callback_state(raw_state):
    if not raw_state or len(raw_state) > GOOGLE_CALENDAR_OAUTH_MAX_STATE_LENGTH:
        raise StaleGoogleCalendarOAuthAttempt("Google Calendar OAuth state is missing or too large")
    try:
        payload = signing.loads(
            raw_state,
            salt=GOOGLE_CALENDAR_OAUTH_STATE_SALT,
            max_age=GOOGLE_CALENDAR_OAUTH_ATTEMPT_SECONDS,
        )
        required_keys = {
            "attempt_generation",
            "connection_id",
            "lifecycle_generation",
            "workspace_id",
            "workspace_slug",
        }
        if not isinstance(payload, dict) or set(payload) != required_keys:
            raise signing.BadSignature("Unexpected Google Calendar state payload")
        if not isinstance(payload["lifecycle_generation"], int) or payload["lifecycle_generation"] < 0:
            raise signing.BadSignature("Invalid Google Calendar lifecycle generation")
        for key in ("attempt_generation", "connection_id", "workspace_id", "workspace_slug"):
            if not isinstance(payload[key], str) or not payload[key]:
                raise signing.BadSignature(f"Invalid Google Calendar state field: {key}")
        return payload
    except signing.BadSignature as exc:
        raise StaleGoogleCalendarOAuthAttempt("Google Calendar OAuth state is invalid or expired") from exc


def _session_owns_attempt(request, payload):
    session_attempt = request.session.get(GOOGLE_CALENDAR_OAUTH_SESSION_KEY)
    return isinstance(session_attempt, dict) and session_attempt == payload


def _consume_session_attempt(request, payload):
    if _session_owns_attempt(request, payload):
        request.session.pop(GOOGLE_CALENDAR_OAUTH_SESSION_KEY, None)


def _assert_attempt_owner(connection, request, payload):
    if (
        str(connection.id) != payload["connection_id"]
        or str(connection.member_id) != str(request.user.id)
        or str(connection.workspace_integration.workspace_id) != payload["workspace_id"]
        or connection.oauth_state != payload["attempt_generation"]
        or connection.lifecycle_generation != payload["lifecycle_generation"]
        or not connection.oauth_attempt_expires_at
        or connection.oauth_attempt_expires_at <= timezone.now()
        or not settings.GOOGLE_CALENDAR_RELEASED
        or not connection.workspace_integration.config.get("enabled", False)
    ):
        raise StaleGoogleCalendarOAuthAttempt("Google Calendar OAuth callback no longer owns this attempt")


@transaction.atomic
def _load_callback_attempt(request, payload):
    connection = lock_google_calendar_connection(payload["connection_id"])
    connection.workspace_integration = WorkspaceIntegration.objects.select_for_update().get(
        id=connection.workspace_integration_id
    )
    _assert_attempt_owner(connection, request, payload)
    return connection.oauth_code_verifier, connection.oauth_redirect_uri


@transaction.atomic
def _abandon_callback_attempt(request, payload):
    connection = lock_google_calendar_connection(payload["connection_id"])
    connection.workspace_integration = WorkspaceIntegration.objects.select_for_update().get(
        id=connection.workspace_integration_id
    )
    _assert_attempt_owner(connection, request, payload)
    _clear_and_save_oauth_attempt(connection)


@transaction.atomic
def _dispose_callback_grant(connection_id, grant, *, account_id=""):
    lock_google_calendar_connection(connection_id)
    return revoke_rejected_google_calendar_grant(grant, account_id=account_id)


@transaction.atomic
def _complete_callback(request, payload, grant, identity):
    connection = lock_google_calendar_connection(payload["connection_id"])
    connection.workspace_integration = WorkspaceIntegration.objects.select_for_update().get(
        id=connection.workspace_integration_id
    )
    _assert_attempt_owner(connection, request, payload)

    if connection.provider_account_id and connection.provider_account_id != identity.account_id:
        _clear_and_save_oauth_attempt(connection)
        if not google_calendar_account_has_live_grant(identity.account_id):
            revoke_rejected_google_calendar_grant(grant)
        return None

    refresh_token = grant.refresh_token or connection.refresh_token
    command = apply_google_calendar_oauth_success(
        connection.id,
        payload["lifecycle_generation"],
        provider_account_id=identity.account_id,
        provider_email=identity.email,
        access_token=grant.access_token,
        refresh_token=refresh_token,
        token_expires_at=grant.token_expires_at,
        scopes=grant.scopes,
    )
    lifecycle_task = current_app.signature(GOOGLE_CALENDAR_LIFECYCLE_TASK)
    enqueue_google_calendar_task_on_commit(
        lifecycle_task,
        str(command.connection_id),
        command.generation,
    )
    return command


class GoogleCalendarOAuthStartEndpoint(BaseAPIView):
    """Start one generation-bound member Calendar consent attempt."""

    permission_classes = [WorkspaceMemberPermission]

    def get_permissions(self):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return [IsAuthenticated()]
        return super().get_permissions()

    @transaction.atomic
    def get(self, request, slug):
        if not settings.GOOGLE_CALENDAR_RELEASED:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        try:
            credentials = get_google_calendar_oauth_credentials()
        except GoogleCalendarOAuthConfigurationError:
            return Response(
                {"error": "google_calendar_credentials_incomplete"},
                status=status.HTTP_409_CONFLICT,
            )

        integration = Integration.objects.filter(provider="google_calendar").first()
        if integration is None:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        workspace_id = WorkspaceMember.objects.get(
            workspace__slug=slug,
            member=request.user,
            is_active=True,
        ).workspace_id
        locked_connections = lock_google_calendar_workspace_connections(workspace_id)
        workspace_integration = (
            WorkspaceIntegration.objects.select_for_update()
            .filter(workspace__slug=slug, integration=integration)
            .first()
        )
        if workspace_integration is None or not workspace_integration.config.get("enabled", False):
            return Response(
                {"error": "google_calendar_disabled"},
                status=status.HTTP_409_CONFLICT,
            )

        connection = next(
            (item for item in locked_connections if item.member_id == request.user.id),
            None,
        )
        if connection is None:
            connection = GoogleCalendarConnection.objects.create(
                workspace_integration=workspace_integration,
                member=request.user,
            )
            connection = lock_google_calendar_connection(connection.id)
        if connection.status == GoogleCalendarConnection.Status.CLEANUP_PENDING:
            return Response(
                {"error": "google_calendar_disconnect_in_progress"},
                status=status.HTTP_409_CONFLICT,
            )

        attempt_generation = secrets.token_urlsafe(32)
        code_verifier, code_challenge = create_google_calendar_pkce_pair()
        redirect_uri = request.build_absolute_uri(reverse("google-calendar-oauth-callback"))
        expires_at = timezone.now() + timedelta(seconds=GOOGLE_CALENDAR_OAUTH_ATTEMPT_SECONDS)
        payload = {
            "attempt_generation": attempt_generation,
            "connection_id": str(connection.id),
            "lifecycle_generation": connection.lifecycle_generation,
            "workspace_id": str(workspace_integration.workspace_id),
            "workspace_slug": slug,
        }
        signed_state = signing.dumps(payload, salt=GOOGLE_CALENDAR_OAUTH_STATE_SALT, compress=True)

        connection.oauth_state = attempt_generation
        connection.oauth_code_verifier = code_verifier
        connection.oauth_redirect_uri = redirect_uri
        connection.oauth_attempt_expires_at = expires_at
        connection.save(
            update_fields=[
                "oauth_state",
                "oauth_code_verifier",
                "oauth_redirect_uri",
                "oauth_attempt_expires_at",
                "updated_at",
            ]
        )
        request.session[GOOGLE_CALENDAR_OAUTH_SESSION_KEY] = payload
        return HttpResponseRedirect(
            build_google_calendar_consent_url(
                credentials,
                redirect_uri=redirect_uri,
                state=signed_state,
                code_challenge=code_challenge,
            )
        )


class GoogleCalendarOAuthCallbackEndpoint(BaseAPIView):
    """Validate and commit one exact member Calendar consent generation."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        raw_state = request.GET.get("state", "")
        try:
            payload = _decode_callback_state(raw_state)
        except StaleGoogleCalendarOAuthAttempt:
            return _calendar_redirect(request, error="google_calendar_oauth_invalid_state")

        workspace_slug = payload["workspace_slug"]
        if not _session_owns_attempt(request, payload):
            return _calendar_redirect(
                request,
                workspace_slug=workspace_slug,
                error="google_calendar_oauth_stale",
            )

        if request.GET.get("error") or not request.GET.get("code"):
            try:
                _abandon_callback_attempt(request, payload)
            except (GoogleCalendarOAuthError, GoogleCalendarConnection.DoesNotExist):
                pass
            _consume_session_attempt(request, payload)
            return _calendar_redirect(
                request,
                workspace_slug=workspace_slug,
                error="google_calendar_oauth_abandoned",
            )

        try:
            code_verifier, redirect_uri = _load_callback_attempt(request, payload)
        except (GoogleCalendarOAuthError, GoogleCalendarConnection.DoesNotExist):
            _consume_session_attempt(request, payload)
            return _calendar_redirect(
                request,
                workspace_slug=workspace_slug,
                error="google_calendar_oauth_stale",
            )

        try:
            credentials = get_google_calendar_oauth_credentials()
            grant = exchange_google_calendar_code(
                credentials,
                code=request.GET["code"],
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
            )
        except GoogleCalendarOAuthError:
            try:
                _abandon_callback_attempt(request, payload)
            except (GoogleCalendarOAuthError, GoogleCalendarConnection.DoesNotExist):
                pass
            _consume_session_attempt(request, payload)
            return _calendar_redirect(
                request,
                workspace_slug=workspace_slug,
                error="google_calendar_oauth_exchange_failed",
            )

        try:
            validate_google_calendar_grant_scopes(grant)
        except GoogleCalendarOAuthScopeError:
            try:
                _abandon_callback_attempt(request, payload)
            except (GoogleCalendarOAuthError, GoogleCalendarConnection.DoesNotExist):
                pass
            _dispose_callback_grant(payload["connection_id"], grant)
            _consume_session_attempt(request, payload)
            return _calendar_redirect(
                request,
                workspace_slug=workspace_slug,
                error="google_calendar_oauth_incomplete_scope",
            )

        try:
            identity = get_google_calendar_identity(grant)
        except GoogleCalendarOAuthIdentityError:
            try:
                _abandon_callback_attempt(request, payload)
            except (GoogleCalendarOAuthError, GoogleCalendarConnection.DoesNotExist):
                pass
            _dispose_callback_grant(payload["connection_id"], grant)
            _consume_session_attempt(request, payload)
            return _calendar_redirect(
                request,
                workspace_slug=workspace_slug,
                error="google_calendar_oauth_identity_failed",
            )

        try:
            command = _complete_callback(request, payload, grant, identity)
        except (GoogleCalendarLifecycleError, GoogleCalendarOAuthError, GoogleCalendarConnection.DoesNotExist):
            _dispose_callback_grant(payload["connection_id"], grant, account_id=identity.account_id)
            _consume_session_attempt(request, payload)
            return _calendar_redirect(
                request,
                workspace_slug=workspace_slug,
                error="google_calendar_oauth_stale",
            )

        _consume_session_attempt(request, payload)
        if command is None:
            return _calendar_redirect(
                request,
                workspace_slug=workspace_slug,
                error="account_switch_requires_disconnect",
            )
        return _calendar_redirect(request, workspace_slug=workspace_slug)
