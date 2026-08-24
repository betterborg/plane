from django.db import migrations
from django.db.models import OuterRef, Subquery


def snapshot_cycle_timezones(apps, schema_editor):
    Cycle = apps.get_model("db", "Cycle")
    Project = apps.get_model("db", "Project")
    database_alias = schema_editor.connection.alias

    # A non-UTC cycle timezone is an explicit historical value and must be preserved.
    # A UTC cycle in a UTC project already has the deterministic snapshot we need.
    # For ambiguous default-UTC rows, snapshot the project's timezone at rollout;
    # this is a compatibility fallback, not recovery of the originally entered date.
    project_timezone = Project.objects.using(database_alias).filter(pk=OuterRef("project_id")).values("timezone")[:1]
    (
        Cycle.objects.using(database_alias)
        .filter(timezone="UTC")
        .exclude(project__timezone="UTC")
        .update(timezone=Subquery(project_timezone))
    )


class Migration(migrations.Migration):
    dependencies = [
        ("db", "0129_project_google_calendar_sync"),
    ]

    operations = [
        migrations.RunPython(snapshot_cycle_timezones, migrations.RunPython.noop),
    ]
