import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("organizations", "0002_vehicle"),
    ]

    operations = [
        migrations.CreateModel(
            name="DatasetScopeState",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("dataset_type_code", models.CharField(max_length=100)),
                ("source_revision", models.PositiveBigIntegerField(default=0)),
                ("dirty_since", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "department",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dataset_scopes",
                        to="organizations.department",
                    ),
                ),
                (
                    "station",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dataset_scopes",
                        to="organizations.station",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="DatasetPublication",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("dataset_type_code", models.CharField(max_length=100)),
                (
                    "scope_state",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="publications",
                        to="publications.datasetscopestate",
                    ),
                ),
                ("version_number", models.PositiveBigIntegerField()),
                ("schema_version", models.PositiveIntegerField()),
                ("source_revision", models.PositiveBigIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("BUILDING", "Building"),
                            ("READY_FOR_REVIEW", "Ready for review"),
                            ("PUBLISHED", "Published"),
                            ("FAILED", "Failed"),
                            ("SUPERSEDED", "Superseded"),
                            ("REJECTED", "Rejected"),
                            ("OBSOLETE", "Obsolete"),
                        ],
                        default="BUILDING",
                        max_length=20,
                    ),
                ),
                ("build_summary", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("build_error", models.TextField(blank=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="created_dataset_publications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "department",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dataset_publications",
                        to="organizations.department",
                    ),
                ),
                (
                    "published_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="published_dataset_publications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "station",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="dataset_publications",
                        to="organizations.station",
                    ),
                ),
                (
                    "supersedes",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="superseded_by",
                        to="publications.datasetpublication",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="PublicationJob",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("dataset_type_code", models.CharField(max_length=100)),
                ("attempt_count", models.PositiveSmallIntegerField(default=0)),
                ("source_revision", models.PositiveBigIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("RUNNING", "Running"),
                            ("SUCCEEDED", "Succeeded"),
                            ("FAILED", "Failed"),
                            ("OBSOLETE", "Obsolete"),
                        ],
                        default="PENDING",
                        max_length=10,
                    ),
                ),
                (
                    "trigger_type",
                    models.CharField(
                        choices=[("USER_REQUEST", "User request"), ("DATA_CHANGE", "Data change")],
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("heartbeat_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("error_message", models.CharField(blank=True, max_length=2000)),
                ("error_category", models.CharField(blank=True, max_length=32)),
                (
                    "build_publication",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="build_jobs",
                        to="publications.datasetpublication",
                    ),
                ),
                (
                    "department",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="publication_jobs",
                        to="organizations.department",
                    ),
                ),
                (
                    "scope_state",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="publication_jobs",
                        to="publications.datasetscopestate",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="requested_publication_jobs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "station",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="publication_jobs",
                        to="organizations.station",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="PublicationActivation",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[("PUBLISH", "Publish"), ("ROLLBACK", "Rollback")], max_length=12
                    ),
                ),
                ("activated_at", models.DateTimeField(auto_now_add=True)),
                (
                    "activated_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="publication_activations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "previous_publication",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="replaced_by_activations",
                        to="publications.datasetpublication",
                    ),
                ),
                (
                    "publication",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="activations",
                        to="publications.datasetpublication",
                    ),
                ),
                (
                    "scope_state",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="publication_activations",
                        to="publications.datasetscopestate",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="datasetpublication",
            constraint=models.UniqueConstraint(
                fields=("department", "station", "dataset_type_code", "version_number"),
                name="unique_dataset_publication_version",
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name="datasetpublication",
            constraint=models.CheckConstraint(
                condition=(
                    Q(
                        ("dataset_type_code__in", ("department_hydrants", "department_fire_plans")),
                        ("station__isnull", True),
                    )
                    | Q(("dataset_type_code", "station_personnel"), ("station__isnull", False))
                ),
                name="registered_dataset_publication_scope",
            ),
        ),
        migrations.AddField(
            model_name="datasetscopestate",
            name="current_published_publication",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="current_for_scopes",
                to="publications.datasetpublication",
            ),
        ),
        migrations.AddField(
            model_name="datasetscopestate",
            name="latest_built_publication",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="latest_for_scopes",
                to="publications.datasetpublication",
            ),
        ),
        migrations.AddConstraint(
            model_name="datasetpublication",
            constraint=models.UniqueConstraint(
                condition=Q(("status", "PUBLISHED")),
                fields=("department", "station", "dataset_type_code"),
                name="one_current_published_dataset_publication",
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name="datasetscopestate",
            constraint=models.UniqueConstraint(
                fields=("department", "station", "dataset_type_code"),
                name="unique_dataset_scope_state",
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name="datasetscopestate",
            constraint=models.CheckConstraint(
                condition=(
                    Q(
                        ("dataset_type_code__in", ("department_hydrants", "department_fire_plans")),
                        ("station__isnull", True),
                    )
                    | Q(("dataset_type_code", "station_personnel"), ("station__isnull", False))
                ),
                name="registered_dataset_scope_state_scope",
            ),
        ),
        migrations.AddIndex(
            model_name="publicationjob",
            index=models.Index(fields=["status", "created_at"], name="pub_job_status_created_idx"),
        ),
        migrations.AddConstraint(
            model_name="publicationjob",
            constraint=models.UniqueConstraint(
                condition=Q(("status__in", ("PENDING", "RUNNING"))),
                fields=("department", "station", "dataset_type_code"),
                name="one_active_publication_job_per_scope",
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name="publicationjob",
            constraint=models.CheckConstraint(
                condition=(
                    Q(
                        ("dataset_type_code__in", ("department_hydrants", "department_fire_plans")),
                        ("station__isnull", True),
                    )
                    | Q(("dataset_type_code", "station_personnel"), ("station__isnull", False))
                ),
                name="registered_publication_job_scope",
            ),
        ),
    ]
