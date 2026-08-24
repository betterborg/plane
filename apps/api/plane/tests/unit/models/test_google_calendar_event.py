# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from plane.db.models import GoogleCalendarEvent
from plane.tests.factories import GoogleCalendarConnectionFactory


@pytest.mark.unit
@pytest.mark.django_db
class TestGoogleCalendarEvent:
    def test_connection_cannot_correlate_a_work_item_twice(self):
        calendar_connection = GoogleCalendarConnectionFactory()
        work_item_id = uuid4()
        GoogleCalendarEvent.objects.create(
            connection=calendar_connection,
            entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
            entity_id=work_item_id,
            google_event_id="plane-work-item-event",
            payload_hash="a" * 64,
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            GoogleCalendarEvent.objects.create(
                connection=calendar_connection,
                entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
                entity_id=work_item_id,
                google_event_id="another-work-item-event",
                payload_hash="b" * 64,
            )

        assert GoogleCalendarEvent.objects.filter(connection=calendar_connection).count() == 1

    def test_connection_cannot_reuse_a_google_event_for_cycles(self):
        calendar_connection = GoogleCalendarConnectionFactory()
        GoogleCalendarEvent.objects.create(
            connection=calendar_connection,
            entity_type=GoogleCalendarEvent.EntityType.CYCLE,
            entity_id=uuid4(),
            google_event_id="plane-cycle-event",
            payload_hash="c" * 64,
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            GoogleCalendarEvent.objects.create(
                connection=calendar_connection,
                entity_type=GoogleCalendarEvent.EntityType.CYCLE,
                entity_id=uuid4(),
                google_event_id="plane-cycle-event",
                payload_hash="d" * 64,
            )

        assert GoogleCalendarEvent.objects.filter(connection=calendar_connection).count() == 1

    def test_correlation_identity_is_scoped_to_the_connection(self):
        first_connection = GoogleCalendarConnectionFactory()
        second_connection = GoogleCalendarConnectionFactory()
        entity_id = uuid4()
        last_synced_at = timezone.now()

        first_event = GoogleCalendarEvent.objects.create(
            connection=first_connection,
            entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
            entity_id=entity_id,
            google_event_id="shared-deterministic-event",
            payload_hash="e" * 64,
            last_synced_at=last_synced_at,
        )
        second_event = GoogleCalendarEvent.objects.create(
            connection=second_connection,
            entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
            entity_id=entity_id,
            google_event_id="shared-deterministic-event",
            payload_hash="e" * 64,
            last_synced_at=last_synced_at,
        )

        first_event.refresh_from_db()
        assert GoogleCalendarEvent.objects.filter(pk__in=(first_event.pk, second_event.pk)).count() == 2
        assert first_event.payload_hash == "e" * 64
        assert first_event.last_synced_at == last_synced_at

    def test_reconciliation_observation_fields_persist(self):
        calendar_connection = GoogleCalendarConnectionFactory(calendar_generation=7)
        event = GoogleCalendarEvent.objects.create(
            connection=calendar_connection,
            entity_type=GoogleCalendarEvent.EntityType.WORK_ITEM,
            entity_id=uuid4(),
            google_event_id="observed-provider-event",
            payload_hash="f" * 64,
            calendar_generation=calendar_connection.calendar_generation,
            provider_etag='"provider-etag"',
            provider_payload_hash="1" * 64,
            provider_status="confirmed",
        )

        event.refresh_from_db()
        assert event.calendar_generation == 7
        assert event.provider_etag == '"provider-etag"'
        assert event.provider_payload_hash == "1" * 64
        assert event.provider_status == "confirmed"
