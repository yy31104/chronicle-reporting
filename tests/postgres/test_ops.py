"""Actual pg_dump/pg_restore and independent checksum-failure rehearsal."""

import json
from uuid import UUID, uuid4

import pytest
from psycopg import sql

from chronicle import db as connections
from chronicle import objects, store
from chronicle.demo import run
from chronicle.errors import ContractError
from chronicle.ops import backup, restore, restore_rehearsal, verify_backup


@pytest.fixture(scope="module")
def database():
    name = "chronicle_test_ops_" + uuid4().hex
    with connections.connect("admin") as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER chronicle_owner").format(sql.Identifier(name))
        )
    try:
        with connections.connect("admin", database=name) as admin:
            connections.migrate(admin)
        yield name
    finally:
        with connections.connect("admin") as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_real_backup_restore_retained_reports_and_corruption(db, tmp_path):
    documents = run(connect=db.connect, dataset_id=db.dataset_id)
    managed = db.dataset_id + "-managed"
    store.create_dataset(db.importer, managed)
    store.import_batch(db.importer, managed, "B1", ())
    root = tmp_path / "objects"
    staged = objects.stage_bytes(
        db.publisher,
        managed,
        b"managed backup payload",
        {"decision_state": "approved_scoped", "permitted_uses": ["local_ingest", "local_analysis"]},
        {"synthetic": True},
        root=root,
    )
    objects.attach_batch_object(db.publisher, managed, "B1", staged)
    objects.publish_snapshot(db.publisher, managed, "S1", ["B1"], None, uuid4(), root=root)
    destination = tmp_path / "backup"
    saved = backup(destination, database=db.database, root=tmp_path / "objects")
    assert len(saved["reports"]) >= 4
    assert saved["published_object_count"] == 2
    result = restore_rehearsal(destination)
    assert result["status"] == "verified"
    assert result["reports_verified"] == len(saved["reports"])
    assert result["objects_verified"] == 2
    assert (
        db.admin.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (result["database"],)
        ).fetchone()
        is None
    )
    # The original development/test database is still readable after the rehearsal.
    from chronicle.reports import replay_report

    report = documents["reports"][0]["report"]
    assert replay_report(db.reader, UUID(report["report_id"])) == report
    # Recovery is retained and usable independently of the backup object directory.
    recovered = "chronicle_recovery_" + uuid4().hex
    target = tmp_path / "recovered-objects"
    try:
        restored = restore(destination, into=recovered, object_root=target)
        assert restored["reports_verified"] == len(saved["reports"])
        assert restored["configuration"]["CHRONICLE_DB_NAME"] == recovered
        with connections.connect("reader", database=recovered) as reader:
            assert replay_report(reader, UUID(report["report_id"])) == report
            assert not reader.execute(
                "SELECT has_database_privilege(current_user,current_database(),'TEMP')"
            ).fetchone()[0]
            assert objects.verify_closure(reader, root=target) == []
        with pytest.raises(ContractError) as error:
            restore(destination, into=recovered, object_root=tmp_path / "must-not-exist")
        assert error.value.code == "RESTORE_TARGET_EXISTS"
        assert not (tmp_path / "must-not-exist").exists()
        with pytest.raises(ContractError) as error:
            restore(destination, into="chronicle_unused", object_root=target)
        assert error.value.code == "RESTORE_TARGET_EXISTS"
    finally:
        db.admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(recovered))
        )
    (destination / "database.dump").write_bytes(b"broken backup")
    with pytest.raises(ContractError) as error:
        restore_rehearsal(destination)
    assert error.value.code == "BACKUP_CORRUPT"


def test_backup_manifest_path_escape_rejected(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "backup_schema": "chronicle.backup.v1",
                "files": {"database.dump": "bad", "../../secret": "bad"},
            }
        )
    )
    with pytest.raises(ContractError):
        verify_backup(tmp_path)
