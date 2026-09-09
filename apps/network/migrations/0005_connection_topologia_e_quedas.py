"""Topologia promovida pro login + eventos de queda (#143).

Só schema, e de propósito: **não há migração de dados**. As colunas nascem
vazias e o primeiro sync as preenche — a ferramenta é de tempo real, não
responde pergunta sobre o passado, e recuperar de `raw_extras` o que o adapter
reescreve minutos depois não compraria nada.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('customers', '0004_alter_contract_source_type_and_more'),
        ('network', '0004_networkelement'),
    ]

    operations = [
        migrations.CreateModel(
            name='ConnectionDropEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('login', models.CharField(blank=True, default='', max_length=128)),
                ('dropped_at', models.DateTimeField()),
                ('restored_at', models.DateTimeField(blank=True, null=True)),
                ('reason', models.CharField(blank=True, default='', max_length=64)),
                ('cto_external_id', models.CharField(blank=True, default='', max_length=128)),
                ('cto_port', models.CharField(blank=True, default='', max_length=32)),
                ('pon_external_id', models.CharField(blank=True, default='', max_length=128)),
                ('transmitter_external_id', models.CharField(blank=True, default='', max_length=128)),
                ('latitude', models.FloatField(blank=True, null=True)),
                ('longitude', models.FloatField(blank=True, null=True)),
                ('monthly_amount', models.DecimalField(blank=True, decimal_places=2, help_text='MRR do contrato no momento da queda — base do MRR afetado.', max_digits=12, null=True)),
            ],
            options={
                'verbose_name': 'Evento de queda',
                'verbose_name_plural': 'Eventos de queda',
            },
        ),
        migrations.AddField(
            model_name='connection',
            name='concentrator_external_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='connection',
            name='cto_external_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='connection',
            name='cto_port',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='connection',
            name='disconnect_reason',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='connection',
            name='last_disconnection_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='connection',
            name='latitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='connection',
            name='longitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='connection',
            name='pon_external_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='connection',
            name='transmitter_external_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='historicalconnection',
            name='concentrator_external_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='historicalconnection',
            name='cto_external_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='historicalconnection',
            name='cto_port',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='historicalconnection',
            name='disconnect_reason',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='historicalconnection',
            name='last_disconnection_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='historicalconnection',
            name='latitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='historicalconnection',
            name='longitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='historicalconnection',
            name='pon_external_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='historicalconnection',
            name='transmitter_external_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddIndex(
            model_name='connection',
            index=models.Index(fields=['organization', 'cto_external_id'], name='network_con_organiz_077c51_idx'),
        ),
        migrations.AddIndex(
            model_name='connection',
            index=models.Index(fields=['organization', 'pon_external_id'], name='network_con_organiz_81db62_idx'),
        ),
        migrations.AddIndex(
            model_name='connection',
            index=models.Index(fields=['organization', 'last_disconnection_at'], name='network_con_organiz_8f0c3f_idx'),
        ),
        migrations.AddField(
            model_name='connectiondropevent',
            name='connection',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='drop_events', to='network.connection'),
        ),
        migrations.AddField(
            model_name='connectiondropevent',
            name='customer',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='connection_drop_events', to='customers.customer'),
        ),
        migrations.AddField(
            model_name='connectiondropevent',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='tenancy.organization', verbose_name='Organização'),
        ),
        migrations.AddIndex(
            model_name='connectiondropevent',
            index=models.Index(fields=['organization', 'dropped_at'], name='network_con_organiz_43e722_idx'),
        ),
        migrations.AddIndex(
            model_name='connectiondropevent',
            index=models.Index(fields=['organization', 'restored_at'], name='network_con_organiz_907376_idx'),
        ),
        migrations.AddConstraint(
            model_name='connectiondropevent',
            constraint=models.UniqueConstraint(condition=models.Q(('restored_at__isnull', True)), fields=('organization', 'connection'), name='unique_open_drop_per_connection'),
        ),
    ]
