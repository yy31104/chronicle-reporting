"""A real backup/restore preserves acquired bytes needed by an incomplete request."""

import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import sql

from chronicle import db as connections
from chronicle import ingestion, objects, sources
from chronicle.ops import backup, restore_rehearsal
from tests.postgres.conftest import database as database_fixture


@pytest.fixture
def database():
    yield from database_fixture.__wrapped__()


def test_restore_resumes_acquisition_with_original_receipt_without_refetch(
    db, tmp_path, monkeypatch
):
    root = tmp_path / "original-objects"
    monkeypatch.setenv("CHRONICLE_OBJECT_ROOT", str(root))
    source_id, request = "frb-2024-02-15", uuid4()
    received = datetime(2026, 10, 29, 13, 18, 9, tzinfo=UTC)
    payload = (Path(__file__).parents[1] / "fixtures/sources/frb-2024-02-15.html").read_bytes()
    calls = []

    def fetch(key, batch):
        calls.append(key)
        return sources.parse_payload(key, payload, received, batch)

    def stop(phase):
        if phase == "acquisition_retained":
            raise RuntimeError("stop after retaining acquisition")

    monkeypatch.setattr(sources, "fetch_and_parse", fetch)
    with pytest.raises(RuntimeError, match="stop after retaining"):
        ingestion.import_sources(
            db.importer, db.publisher, db.dataset_id, "S1", (source_id,), None, request, _fault=stop
        )
    assert db.reader.execute("SELECT count(*) FROM chronicle.artifact_publication").fetchone() == (
        0,
    )
    destination = tmp_path / "backup"
    manifest = backup(destination, database=db.database, root=root)
    assert manifest["published_object_count"] == 0
    assert manifest["retained_object_count"] == 1
    result = restore_rehearsal(destination, keep=True)
    restored = result["database"]
    try:
        restored_root = tmp_path / "restored-objects"
        shutil.copytree(destination / "objects", restored_root)
        monkeypatch.setenv("CHRONICLE_OBJECT_ROOT", str(restored_root))
        monkeypatch.setattr(
            sources,
            "fetch_and_parse",
            lambda *_: pytest.fail("A restored acquisition must not refetch"),
        )
        with (
            connections.connect("importer", database=restored) as importer,
            connections.connect("publisher", database=restored) as publisher,
            connections.connect("reader", database=restored) as reader,
        ):
            assert (
                ingestion.import_sources(
                    importer, publisher, db.dataset_id, "S1", (source_id,), None, request
                )
                == "S1"
            )
            assert reader.execute(
                "SELECT DISTINCT received_at FROM chronicle.revision_occurrence"
            ).fetchall() == [(received,)]
            assert reader.execute(
                "SELECT count(*) FROM chronicle.staging_ticket WHERE state='active'"
            ).fetchone() == (0,)
            assert objects.verify_closure(reader, root=restored_root) == []
        assert calls == [source_id]
        # The source catalog is still the uncompleted pre-backup request.
        assert db.reader.execute(
            "SELECT count(*) FROM chronicle.artifact_publication"
        ).fetchone() == (0,)
    finally:
        with connections.connect("admin") as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(restored)))
