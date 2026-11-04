from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ui_automation', '0008_aicase_source_ref'),
    ]

    operations = [
        migrations.AddField(
            model_name='testscript',
            name='status',
            field=models.CharField(
                blank=True, default='confirmed', max_length=20,
                choices=[('draft', '草稿'), ('confirmed', '已确认')],
                verbose_name='生成状态',
            ),
        ),
    ]
