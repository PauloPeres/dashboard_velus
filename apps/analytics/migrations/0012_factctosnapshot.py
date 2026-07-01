"""Snapshot periódico de CTOs FTTH — série temporal de ocupação."""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("tenancy", "0001_initial"),
        ("analytics", "0011_qareview"),
    ]

    operations = [
        migrations.CreateModel(
            name="FactCtoSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("captured_at", models.DateTimeField(db_index=True)),
                ("total_ctos", models.IntegerField(default=0)),
                ("total_ports", models.IntegerField(default=0)),
                ("occupied_ports", models.IntegerField(default=0)),
                ("free_ports", models.IntegerField(default=0)),
                ("occupancy_pct", models.DecimalField(decimal_places=1, default=0, max_digits=5)),
                # Detalhamento por projeto — JSON com lista de dicts
                # [{project, project_id, cto_count, occupied, free, total_ports, occupancy_pct}]
                ("by_project", models.JSONField(default=list)),
                ("organization", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="cto_snapshots",
                    to="tenancy.organization",
                )),
            ],
            options={
                "verbose_name": "Fato: snapshot de CTO",
                "verbose_name_plural": "Fatos: snapshots de CTO",
                "indexes": [
                    models.Index(fields=["organization", "captured_at"]),
                ],
            },
        ),
    ]
