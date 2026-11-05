"""Real-server harness: unique database per suite, unique dataset per test."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg import sql

from chronicle import db as connections
from chronicle import store
from tests.support import revision_from_fixture


def pytest_collection_modifyitems(items):
    for item in items:
        if "/tests/postgres/" in str(item.path):
            item.add_marker(pytest.mark.postgres)


@pytest.fixture(scope="session")
def database():
    name = f"chronicle_test_{uuid4().hex}"
    with connections.connect("admin") as admin:
        connections.provision_roles(admin)
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


@pytest.fixture
def db(database):
    opened = []

    def connect(role):
        conn = connections.connect(role, database=database)
        opened.append(conn)
        return conn

    result = SimpleNamespace(
        dataset_id=f"test-{uuid4().hex}",
        connect=connect,
        database=database,
        admin=connect("admin"),
        importer=connect("importer"),
        publisher=connect("publisher"),
        reader=connect("reader"),
    )
    store.create_dataset(result.importer, result.dataset_id)
    try:
        yield result
    finally:
        for conn in opened:
            conn.close()


def import_fixture(db, fixture):
    """Load raw golden inputs through production admission, not through the oracle."""
    revisions = tuple(revision_from_fixture(row) for row in fixture["revisions"])
    batches = sorted({receipt.batch_id for row in revisions for receipt in row.occurrences})
    for batch_id in batches:
        batch_rows = tuple(
            replace(row, occurrences=tuple(r for r in row.occurrences if r.batch_id == batch_id))
            for row in revisions
            if any(r.batch_id == batch_id for r in row.occurrences)
        )
        store.import_batch(db.importer, db.dataset_id, batch_id, batch_rows)
    expected_current = None
    fixture_hash = hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()
    for snapshot_id, batch_ids in fixture["snapshots"].items():
        store.publish_snapshot(
            db.publisher, db.dataset_id, snapshot_id, batch_ids, fixture_hash, expected_current
        )
        expected_current = snapshot_id
