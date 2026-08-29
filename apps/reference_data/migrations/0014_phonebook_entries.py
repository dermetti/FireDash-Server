import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("organizations", "0007_department_locale_and_timezone"), ("reference_data", "0013_hydrant_descriptive_location")]

    operations = [
        migrations.CreateModel(
            name="PhonebookEntry",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("first_name", models.CharField(blank=True, max_length=255)), ("last_name", models.CharField(blank=True, max_length=255)),
                ("organization_unit", models.CharField(blank=True, max_length=255)), ("function", models.CharField(blank=True, max_length=255)),
                ("phone_number", models.CharField(max_length=64)), ("created_at", models.DateTimeField(auto_now_add=True)), ("updated_at", models.DateTimeField(auto_now=True)),
                ("department", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="phonebook_entries", to="organizations.department")),
                ("station", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="phonebook_entries", to="organizations.station")),
            ], options={"indexes": [models.Index(fields=["department", "station"], name="reference_d_departm_152299_idx")]},
        ),
        migrations.CreateModel(
            name="PhonebookDuplicateDecision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("first_fingerprint", models.CharField(max_length=64)), ("second_fingerprint", models.CharField(max_length=64)), ("created_at", models.DateTimeField(auto_now_add=True)),
                ("department", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="phonebook_duplicate_decisions", to="organizations.department")),
                ("first_entry", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="duplicate_decisions_as_first", to="reference_data.phonebookentry")),
                ("second_entry", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="duplicate_decisions_as_second", to="reference_data.phonebookentry")),
            ],
        ),
        migrations.AddConstraint(model_name="phonebookduplicatedecision", constraint=models.UniqueConstraint(fields=("first_entry", "second_entry"), name="unique_phonebook_duplicate_pair")),
        migrations.AddConstraint(model_name="phonebookduplicatedecision", constraint=models.CheckConstraint(condition=~models.Q(("first_entry", models.F("second_entry"))), name="phonebook_duplicate_entries_differ")),
    ]
