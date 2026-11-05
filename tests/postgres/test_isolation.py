"""Batch closure isolation guard and a disposable reproduction of the old race."""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from importlib.resources import files
from uuid import uuid4

import pytest
from psycopg import IsolationLevel, errors, sql

from chronicle import db as connections
from chronicle import store
from tests.postgres.test_transactions import batch_rows

SEAL = "SELECT chronicle.seal_batch(%s, %s)"
ADD_RECEIPT = (
    "INSERT INTO chronicle.revision_occurrence "
    "(dataset_id,batch_id,source_namespace,series_key,observation_month,"
    "source_revision_key,received_at) "
    "VALUES (%s,'OPEN','synthetic','employment','2026-01-01','initial',%s)"
)


def _open_batch(conn, dataset_id, expected=0):
    conn.execute(
        "INSERT INTO chronicle.import_batch "
        "(dataset_id,batch_id,content_sha256,expected_occurrence_count) "
        "VALUES (%s,'OPEN',%s,%s)",
        (dataset_id, "a" * 64, expected),
    )


def _batch_state(conn, dataset_id):
    return conn.execute(
        "SELECT state, expected_occurrence_count, "
        "(SELECT count(*) FROM chronicle.revision_occurrence AS o "
        "WHERE o.dataset_id=b.dataset_id AND o.batch_id=b.batch_id) "
        "FROM chronicle.import_batch AS b WHERE dataset_id=%s AND batch_id='OPEN'",
        (dataset_id,),
    ).fetchone()


def test_read_committed_seal_and_exact_replay_succeed(db):
    _open_batch(db.importer, db.dataset_id)
    assert db.importer.execute("SHOW transaction_isolation").fetchone() == ("read committed",)
    db.importer.execute(SEAL, (db.dataset_id, "OPEN"))
    db.importer.execute(SEAL, (db.dataset_id, "OPEN"))
    assert _batch_state(db.reader, db.dataset_id) == ("validated", 0, 0)


def test_read_committed_still_rejects_an_incomplete_batch(db):
    _open_batch(db.importer, db.dataset_id, expected=1)
    with pytest.raises(errors.CheckViolation, match="BATCH_INCOMPLETE"):
        db.importer.execute(SEAL, (db.dataset_id, "OPEN"))
    assert _batch_state(db.reader, db.dataset_id) == ("staging", 1, 0)


@pytest.mark.parametrize(
    "isolation",
    [IsolationLevel.REPEATABLE_READ, IsolationLevel.SERIALIZABLE, IsolationLevel.READ_UNCOMMITTED],
    ids=["repeatable-read", "serializable", "read-uncommitted"],
)
@pytest.mark.parametrize("batch_status", ["staging", "validated", "absent"])
def test_non_read_committed_rejects_before_batch_lookup_or_replay(db, isolation, batch_status):
    _open_batch(db.importer, db.dataset_id)
    if batch_status == "validated":
        db.importer.execute(SEAL, (db.dataset_id, "OPEN"))
    sealer = db.connect("importer")
    sealer.isolation_level = isolation
    target = "DOES_NOT_EXIST" if batch_status == "absent" else "OPEN"
    with pytest.raises(errors.RaiseException) as caught:
        with sealer.transaction():
            sealer.execute(SEAL, (db.dataset_id, target))
    assert caught.value.sqlstate == "P0001"
    assert caught.value.diag.message_primary == "SEAL_REQUIRES_READ_COMMITTED"
    expected_state = "validated" if batch_status == "validated" else "staging"
    assert _batch_state(db.reader, db.dataset_id) == (expected_state, 0, 0)


def test_repeatable_read_guard_rejects_while_writer_holds_batch_lock(db):
    store.import_batch(db.importer, db.dataset_id, "B1", batch_rows())
    _open_batch(db.importer, db.dataset_id, expected=1)
    db.importer.execute(ADD_RECEIPT, (db.dataset_id, "2026-04-01T00:00:00Z"))
    writer, sealer = db.connect("importer"), db.connect("importer")
    sealer.isolation_level = IsolationLevel.REPEATABLE_READ
    with writer.transaction():
        writer.execute(ADD_RECEIPT, (db.dataset_id, "2026-04-02T00:00:00Z"))
        with pytest.raises(errors.RaiseException) as caught:
            with sealer.transaction():
                sealer.execute("SET LOCAL lock_timeout = '250ms'")
                assert _batch_state(sealer, db.dataset_id) == ("staging", 1, 1)
                sealer.execute(SEAL, (db.dataset_id, "OPEN"))
        assert caught.value.sqlstate == "P0001"
        assert caught.value.diag.message_primary == "SEAL_REQUIRES_READ_COMMITTED"
    assert _batch_state(db.reader, db.dataset_id) == ("staging", 1, 2)


def test_original_seal_function_reproduces_repeatable_read_count_race(database):
    """Replay the actual 001 function only in a separately disposable database.

    The observed wrong state anchors why the isolation rejection is necessary;
    removing the guard also breaks the typed-error assertions above.
    """
    name = f"chronicle_test_seal_mutant_{uuid4().hex}"
    original_schema = files("chronicle").joinpath("sql/001_schema.sql").read_text()
    originals = re.findall(
        r"CREATE FUNCTION chronicle\.seal_batch\(.*?\n\$\$;", original_schema, re.DOTALL
    )
    assert len(originals) == 1
    original_function = originals[0].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    with connections.connect("admin", database=database) as setup:
        setup.execute(
            sql.SQL("CREATE DATABASE {} OWNER chronicle_owner").format(sql.Identifier(name))
        )
        try:
            with ExitStack() as stack:
                admin = stack.enter_context(connections.connect("admin", database=name))
                connections.migrate(admin)
                with admin.transaction():
                    admin.execute("SET LOCAL ROLE chronicle_owner")
                    admin.execute(original_function)
                importer = stack.enter_context(connections.connect("importer", database=name))
                writer = stack.enter_context(connections.connect("importer", database=name))
                sealer = stack.enter_context(connections.connect("importer", database=name))
                dataset_id = "old-isolation-probe"
                store.create_dataset(importer, dataset_id)
                store.import_batch(importer, dataset_id, "B1", batch_rows())
                _open_batch(importer, dataset_id, expected=1)
                importer.execute(ADD_RECEIPT, (dataset_id, "2026-04-01T00:00:00Z"))
                sealer.isolation_level = IsolationLevel.REPEATABLE_READ
                with ThreadPoolExecutor(max_workers=1) as pool:
                    with sealer.transaction():
                        with writer.transaction():
                            writer.execute(ADD_RECEIPT, (dataset_id, "2026-04-02T00:00:00Z"))
                            assert _batch_state(sealer, dataset_id) == ("staging", 1, 1)
                            pending = pool.submit(sealer.execute, SEAL, (dataset_id, "OPEN"))
                            deadline = time.monotonic() + 3
                            while time.monotonic() < deadline:
                                blockers = admin.execute(
                                    "SELECT pg_blocking_pids(%s)", (sealer.info.backend_pid,)
                                ).fetchone()[0]
                                if writer.info.backend_pid in blockers:
                                    break
                                time.sleep(0.01)
                            else:
                                pytest.fail("old sealer did not wait on the in-flight batch writer")
                            assert not pending.done()
                        pending.result(timeout=5)
                # The old function checked one snapshot, then sealed two receipts.
                assert _batch_state(importer, dataset_id) == ("validated", 1, 2)
        finally:
            setup.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
