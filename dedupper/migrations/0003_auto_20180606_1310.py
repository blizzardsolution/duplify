# Generated by Django 2.0.5 on 2018-06-06 13:10

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dedupper', '0002_auto_20180606_1257'),
    ]

    operations = [
        migrations.AddField(
            model_name='simple',
            name='closest',
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name='simple',
            name='closest2',
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name='simple',
            name='closest3',
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
    ]
