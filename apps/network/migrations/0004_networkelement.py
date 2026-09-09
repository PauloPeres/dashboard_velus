"""Topologia de rede — NetworkElement (#142).

Separada da migração de conexões porque são duas mudanças lógicas distintas
(AGENT.md §4.3): esta cria a planta, a seguinte promove campos do login.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('network', '0003_alter_bandwidthusage_source_type_and_more'),
        ('tenancy', '0013_alter_historicalorganizationdatasource_capability_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='NetworkElement',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('source_type', models.CharField(choices=[('IXC', 'IXC Soft'), ('OPA', 'Opa! Suite'), ('SGP', 'SGP'), ('CONTAAZUL', 'Conta Azul'), ('CSV', 'CSV upload'), ('FAKE', 'Fake (testes)')], help_text='Sistema externo que originou este registro.', max_length=32)),
                ('kind', models.CharField(choices=[('CTO', 'Caixa de atendimento (CTO)'), ('POP', 'Ponto de presença (POP)'), ('PON', 'Porta PON'), ('OLT', 'Transmissor (OLT)'), ('CABLE', 'Cabo')], max_length=16)),
                ('external_id', models.CharField(help_text='ID do elemento no sistema externo (opaco — string).', max_length=128)),
                ('name', models.CharField(blank=True, default='', max_length=255)),
                ('latitude', models.FloatField(blank=True, null=True)),
                ('longitude', models.FloatField(blank=True, null=True)),
                ('parent_external_id', models.CharField(blank=True, default='', max_length=128)),
                ('parent_kind', models.CharField(blank=True, choices=[('CTO', 'Caixa de atendimento (CTO)'), ('POP', 'Ponto de presença (POP)'), ('PON', 'Porta PON'), ('OLT', 'Transmissor (OLT)'), ('CABLE', 'Cabo')], default='', max_length=16)),
                ('capacity', models.IntegerField(blank=True, null=True)),
                ('address', models.CharField(blank=True, default='', max_length=255)),
                ('project_external_id', models.CharField(blank=True, default='', max_length=128)),
                ('status', models.CharField(blank=True, default='', max_length=32)),
                ('raw_extras', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'verbose_name': 'Elemento de rede',
                'verbose_name_plural': 'Elementos de rede',
            },
        ),
        migrations.AddField(
            model_name='networkelement',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='tenancy.organization', verbose_name='Organização'),
        ),
        migrations.AddIndex(
            model_name='networkelement',
            index=models.Index(fields=['organization', 'kind'], name='network_net_organiz_d85847_idx'),
        ),
        migrations.AddIndex(
            model_name='networkelement',
            index=models.Index(fields=['organization', 'kind', 'parent_external_id'], name='network_net_organiz_de4575_idx'),
        ),
        migrations.AddIndex(
            model_name='networkelement',
            index=models.Index(fields=['organization', 'source_type', 'kind', 'external_id'], name='netelem_lookup_idx'),
        ),
        migrations.AddConstraint(
            model_name='networkelement',
            constraint=models.UniqueConstraint(fields=('organization', 'source_type', 'kind', 'external_id'), name='unique_network_element_per_source'),
        ),
    ]
