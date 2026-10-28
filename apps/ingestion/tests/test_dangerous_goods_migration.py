import uuid

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.accounts.models import User
from apps.organizations.models import Department


@pytest.mark.django_db(transaction=True)
def test_system_goods_migration_removes_only_legacy_data():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    before = [
        (app, "0004_dangerous_goods_import_domain") if app == "ingestion" else (app, name)
        for app, name in latest
    ]
    executor.migrate(before)
    try:
        executor = MigrationExecutor(connection)
        old = executor.loader.project_state(list(executor.loader.applied_migrations)).apps
        get = old.get_model
        registry = get("publications", "DatasetTypeRegistry")
        registry.objects.filter(code="dangerous_goods").update(scope="department")
        user = User.objects.create_user("migration-goods@example.test", "Migration", "password")
        department = Department.objects.create(
            name="Migration Goods", short_code="MG", created_by=user
        )
        Scope = get("publications", "DatasetScopeState")
        scope = Scope.objects.create(
            scope_type="DEPARTMENT",
            department_id=department.id,
            dataset_type_code="dangerous_goods",
            source_revision=1,
        )
        other = Scope.objects.create(
            scope_type="DEPARTMENT",
            department_id=department.id,
            dataset_type_code="department_hydrants",
        )
        Publication = get("publications", "DatasetPublication")
        publication = Publication.objects.create(
            scope_type="DEPARTMENT",
            department_id=department.id,
            dataset_type_code="dangerous_goods",
            scope_state_id=scope.id,
            version_number=1,
            schema_version=1,
            source_revision=1,
        )
        get("publications", "DatasetSourceRevision").objects.create(
            scope_state_id=scope.id, source_revision=1, sha256="a" * 64, byte_size=1, plaintext=b"x"
        )
        get("publications", "PublicationActivation").objects.create(
            scope_state_id=scope.id,
            publication_id=publication.id,
            action="PUBLISH",
            activated_by_id=user.id,
        )
        job = get("publications", "PublicationJob").objects.create(
            scope_state_id=scope.id,
            scope_type="DEPARTMENT",
            department_id=department.id,
            dataset_type_code="dangerous_goods",
            source_revision=1,
            trigger_type="DATA_CHANGE",
            build_publication_id=publication.id,
        )
        from cryptography.hazmat.primitives.asymmetric import ec

        from apps.ingestion.tests.test_dangerous_goods_tablet_contract import _installation

        installation, _ = _installation(
            department=department, actor=user, private_key=ec.generate_private_key(ec.SECP256R1())
        )
        Grant = get("publications", "DatasetKeyGrant")
        grant = Grant.objects.create(
            publication_id=publication.id, app_installation_id=installation.id
        )
        Manifest = get("publications", "SignedManifest")
        manifest = Manifest.objects.create(
            app_installation_id=installation.id,
            state_hash="a" * 64,
            status="READY",
            payload={"datasets": [{"type": "dangerous_goods"}]},
            signature=b"sig",
        )
        other_manifest = Manifest.objects.create(
            app_installation_id=installation.id,
            state_hash="b" * 64,
            status="READY",
            payload={"datasets": [{"type": "department_hydrants"}]},
            signature=b"sig",
        )
        Batch = get("ingestion", "ImportBatch")
        common = dict(
            department_id=department.id,
            actor_id=user.id,
            import_format="json",
            import_mode="merge",
            original_filename="x",
            upload_sha256="a" * 64,
        )
        batch = Batch.objects.create(domain="dangerous_goods", staging_key="legacy", **common)
        other_batch = Batch.objects.create(domain="hydrants", staging_key="other", **common)
        Audit = get("audit", "AuditEvent")
        event = Audit.objects.create(
            action="legacy",
            target_type="import_batch",
            target_uuid=batch.id,
            request_id=uuid.uuid4(),
        )
        unrelated = Audit.objects.create(
            action="unrelated", target_type="other", request_id=uuid.uuid4()
        )
        connection.commit()
        MigrationExecutor(connection).migrate(latest)
        from apps.audit.models import AuditEvent
        from apps.ingestion.models import ImportBatch
        from apps.publications.models import (
            DatasetKeyGrant,
            DatasetScopeState,
            DatasetTypeRegistry,
            PublicationJob,
            SignedManifest,
        )

        assert not DatasetScopeState.objects.filter(pk=scope.pk).exists()
        assert DatasetScopeState.objects.filter(pk=other.pk).exists()
        assert not PublicationJob.objects.filter(pk=job.pk).exists()
        assert not DatasetKeyGrant.objects.filter(pk=grant.pk).exists()
        manifest = SignedManifest.objects.get(pk=manifest.pk)
        assert manifest.status == "OBSOLETE" and manifest.signature is None
        assert SignedManifest.objects.get(pk=other_manifest.pk).status == "READY"
        assert not ImportBatch.objects.filter(pk=batch.pk).exists()
        assert ImportBatch.objects.filter(pk=other_batch.pk).exists()
        assert not AuditEvent.objects.filter(pk=event.pk).exists()
        assert AuditEvent.objects.filter(pk=unrelated.pk).exists()
        assert DatasetTypeRegistry.objects.get(code="dangerous_goods").scope == "system"
        assert (
            DatasetScopeState.objects.filter(
                scope_type="SYSTEM",
                department__isnull=True,
                station__isnull=True,
                dataset_type_code="dangerous_goods",
            ).count()
            == 1
        )
    finally:
        MigrationExecutor(connection).migrate(latest)
