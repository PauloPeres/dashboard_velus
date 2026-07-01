"""Adiciona created_at/updated_at ao FactCtoSnapshot (faltou na 0012)."""

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("analytics", "0012_factctosnapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="factctosnapshot",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, db_index=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="factctosnapshot",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
    ]
