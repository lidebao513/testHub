from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ui_automation', '0006_aiexecutionrecord_assertion_report'),
    ]

    operations = [
        migrations.AddField(
            model_name='aicase',
            name='last_loop',
            field=models.JSONField(blank=True, default=dict, help_text='#183 闭环回流标记：{state, exec_status, assertion_signal, last_record_id, updated_at}', verbose_name='闭环回流标记'),
        ),
    ]
