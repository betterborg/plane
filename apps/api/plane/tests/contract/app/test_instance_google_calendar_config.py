# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import uuid

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from plane.license.models import Instance, InstanceAdmin, InstanceConfiguration
from plane.license.utils.encryption import decrypt_data, encrypt_data
from plane.utils.instance_config_variables.core import google_calendar_config_variables


CALENDAR_CLIENT_ID = "GOOGLE_CALENDAR_CLIENT_ID"
CALENDAR_CLIENT_SECRET = "GOOGLE_CALENDAR_CLIENT_SECRET"
CALENDAR_PROJECT_DEDICATED = "GOOGLE_CALENDAR_IS_PROJECT_DEDICATED"


@pytest.fixture(autouse=True)
def _disable_instance_cache(monkeypatch):
    monkeypatch.setattr("plane.utils.cache.cache.get", lambda _key: None)
    monkeypatch.setattr("plane.utils.cache.cache.delete", lambda _key: True)


@pytest.fixture
def instance(db):
    return Instance.objects.create(
        instance_name="Calendar test instance",
        instance_id=str(uuid.uuid4()),
        current_version="1.0.0",
        domain="http://testserver",
        last_checked_at=timezone.now(),
        is_setup_done=True,
    )


@pytest.fixture
def instance_admin(instance, create_user):
    return InstanceAdmin.objects.create(instance=instance, user=create_user, role=20)


def _create_configurations(client_id, client_secret, project_is_dedicated):
    return InstanceConfiguration.objects.bulk_create(
        [
            InstanceConfiguration(
                key=CALENDAR_CLIENT_ID,
                value=client_id,
                category="GOOGLE_CALENDAR",
                is_encrypted=False,
            ),
            InstanceConfiguration(
                key=CALENDAR_CLIENT_SECRET,
                value=encrypt_data(client_secret),
                category="GOOGLE_CALENDAR",
                is_encrypted=True,
            ),
            InstanceConfiguration(
                key=CALENDAR_PROJECT_DEDICATED,
                value=project_is_dedicated,
                category="GOOGLE_CALENDAR",
                is_encrypted=False,
            ),
        ]
    )


@pytest.mark.contract
class TestGoogleCalendarInstanceConfiguration:
    @pytest.mark.django_db
    @pytest.mark.parametrize(
        ("client_id", "client_secret", "project_is_dedicated", "released", "expected"),
        [
            ("", "calendar-secret", "1", True, False),
            ("calendar-client", "", "1", True, False),
            ("calendar-client", "calendar-secret", "0", True, False),
            ("calendar-client", "calendar-secret", "1", False, False),
            ("calendar-client", "calendar-secret", "1", True, True),
        ],
        ids=["missing-client", "missing-secret", "non-isolated", "unreleased", "complete-released"],
    )
    def test_public_availability_requires_complete_released_configuration(
        self,
        api_client,
        instance,
        client_id,
        client_secret,
        project_is_dedicated,
        released,
        expected,
    ):
        _create_configurations(client_id, client_secret, project_is_dedicated)

        with override_settings(GOOGLE_CALENDAR_RELEASED=released):
            response = api_client.get(reverse("instance"))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["config"]["is_google_calendar_available"] is expected
        assert CALENDAR_CLIENT_ID not in response.data["config"]
        assert CALENDAR_CLIENT_SECRET not in response.data["config"]
        assert CALENDAR_PROJECT_DEDICATED not in response.data["config"]
        assert "google_calendar_client_id" not in response.data["config"]
        assert "google_calendar_client_secret" not in response.data["config"]
        assert "google_calendar_is_project_dedicated" not in response.data["config"]

    @pytest.mark.django_db
    def test_configuration_patch_encrypts_calendar_secret(self, session_client, instance_admin):
        _create_configurations("old-client", "old-secret", "1")

        with override_settings(GOOGLE_CALENDAR_RELEASED=False):
            response = session_client.patch(
                reverse("instance-configuration"),
                {CALENDAR_CLIENT_SECRET: "replacement-secret"},
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK
        configuration = InstanceConfiguration.objects.get(key=CALENDAR_CLIENT_SECRET)
        assert configuration.is_encrypted is True
        assert configuration.value != "replacement-secret"
        assert decrypt_data(configuration.value) == "replacement-secret"

    @pytest.mark.django_db
    def test_released_credentials_reject_entire_configuration_patch(self, session_client, instance_admin):
        _create_configurations("old-client", "old-secret", "1")
        unrelated_configuration = InstanceConfiguration.objects.create(
            key="ENABLE_SIGNUP",
            value="1",
            category="AUTHENTICATION",
            is_encrypted=False,
        )
        original_values = dict(
            InstanceConfiguration.objects.filter(
                key__in=[
                    CALENDAR_CLIENT_ID,
                    CALENDAR_CLIENT_SECRET,
                    CALENDAR_PROJECT_DEDICATED,
                    unrelated_configuration.key,
                ]
            ).values_list("key", "value")
        )

        with override_settings(GOOGLE_CALENDAR_RELEASED=True):
            response = session_client.patch(
                reverse("instance-configuration"),
                {
                    CALENDAR_CLIENT_ID: "new-client",
                    CALENDAR_CLIENT_SECRET: "new-secret",
                    CALENDAR_PROJECT_DEDICATED: "0",
                    unrelated_configuration.key: "0",
                },
                format="json",
            )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data == {"error": "google_calendar_credentials_locked"}
        persisted_values = dict(
            InstanceConfiguration.objects.filter(key__in=original_values).values_list("key", "value")
        )
        assert persisted_values == original_values

    def test_release_gate_defaults_to_false(self):
        assert settings.GOOGLE_CALENDAR_RELEASED is False

    def test_calendar_secret_is_registered_as_encrypted_configuration(self):
        variables_by_key = {item["key"]: item for item in google_calendar_config_variables}

        assert variables_by_key[CALENDAR_CLIENT_SECRET]["is_encrypted"] is True
        assert set(variables_by_key) == {
            CALENDAR_CLIENT_ID,
            CALENDAR_CLIENT_SECRET,
            CALENDAR_PROJECT_DEDICATED,
        }
