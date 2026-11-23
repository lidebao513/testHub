# 地址优先入口：归一 URL → 两侧项目 id 映射表（新增，不改既有迁移）

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('projects', '0002_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='UrlProject',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('normalized_url', models.CharField(max_length=255, unique=True, verbose_name='归一化被测地址')),
                ('scheme', models.CharField(default='http', max_length=16, verbose_name='协议')),
                ('host', models.CharField(max_length=255, verbose_name='主机')),
                ('testgen_project_id', models.PositiveIntegerField(verbose_name='testgen 项目 ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('testhub_project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='url_bindings', to='projects.project', verbose_name='TestHub 项目')),
            ],
            options={
                'db_table': 'url_projects',
                'ordering': ['-created_at'],
                'verbose_name': '地址项目映射',
                'verbose_name_plural': '地址项目映射',
            },
        ),
    ]
