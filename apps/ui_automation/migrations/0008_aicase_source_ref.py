from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ui_automation', '0007_aicase_last_loop'),
    ]

    operations = [
        migrations.AddField(
            model_name='aicase',
            name='source_ref',
            field=models.CharField(
                blank=True, db_index=True, max_length=128, null=True,
                verbose_name='来源指纹',
            ),
        ),
    ]
