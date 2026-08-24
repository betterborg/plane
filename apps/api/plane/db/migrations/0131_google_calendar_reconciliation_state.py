# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import plane.db.models.integration.calendar
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("db", "0130_snapshot_cycle_timezones"),
    ]

    operations = [
        migrations.AddField(
            model_name="googlecalendarconnection",
            name="calendar_generation",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="googlecalendarconnection",
            name="page_token",
            field=plane.db.models.integration.calendar.EncryptedTextField(blank=True),
        ),
        migrations.AddField(
            model_name="googlecalendarconnection",
            name="credential_fingerprint",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="googlecalendarconnection",
            name="reconciliation_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="googlecalendarconnection",
            name="reconciliation_cursor",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="googlecalendarconnection",
            name="reconciliation_lease_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="googlecalendarconnection",
            name="reconciliation_phase",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="googlecalendarconnection",
            name="sync_token",
            field=plane.db.models.integration.calendar.EncryptedTextField(blank=True),
        ),
        migrations.AddField(
            model_name="googlecalendarevent",
            name="calendar_generation",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="googlecalendarevent",
            name="provider_etag",
            field=models.CharField(blank=True, max_length=1024),
        ),
        migrations.AddField(
            model_name="googlecalendarevent",
            name="provider_payload_hash",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="googlecalendarevent",
            name="provider_status",
            field=models.CharField(blank=True, max_length=32),
        ),
    ]
