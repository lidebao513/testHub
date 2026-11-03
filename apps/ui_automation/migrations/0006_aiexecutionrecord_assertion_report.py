from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ui_automation', '0005_aiexecutionrecord_cost_aiexecutionrecord_pre_explore_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='aiexecutionrecord',
            name='assertion_report',
            field=models.JSONField(blank=True, default=dict, help_text='#181 AI/规则/会话三层断言的质量信号（quality_signal/counts/items），不改变执行状态判定', verbose_name='三层断言报告'),
        ),
    ]
