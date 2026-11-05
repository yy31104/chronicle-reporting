"""Atomic imports, idempotency, current-pointer races and immutable context pins."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest
from psycopg import errors

from chronicle import db as connections
from chronicle import store
from chronicle.domain import Occurrence, Query
from chronicle.errors import ContractError
from tests.postgres.conftest import import_fixture
from tests.support import query_from_fixture, revision_from_fixture

G1 = json.loads((Path(__file__).parents[1] / "fixtures/golden/G1.json").read_text())


def batch_rows(batch="B1"):
    return tuple(
        revision_from_fixture(row)
        for row in G1["revisions"]
        if row["occurrences"][0]["batch_id"] == batch
    )


def current(db):
    return db.reader.execute(
        "SELECT current_snapshot_id FROM chronicle.dataset WHERE dataset_id=%s", (db.dataset_id,)
    ).fetchone()[0]


def test_identical_batch_replay_is_order_independent_and_content_bound(db):
    rows = batch_rows()
    store.import_batch(db.importer, db.dataset_id, "B1", rows)
    assert store.import_batch(db.importer, db.dataset_id, "B1", tuple(reversed(rows))) == "B1"
    assert (
        db.reader.execute(
            "SELECT count(*) FROM chronicle.revision_occurrence WHERE dataset_id=%s",
            (db.dataset_id,),
        ).fetchone()[0]
        == 2
    )
    with pytest.raises(ContractError) as caught:
        store.import_batch(
            db.importer, db.dataset_id, "B1", (replace(rows[0], value="999"), rows[1])
        )
    assert caught.value.code == "BATCH_CONFLICT"


def test_new_batch_reobservation_adds_provenance_and_conflict_rolls_back_whole_batch(db):
    import_fixture(db, G1)
    old_context = store.create_context(
        db.reader, db.dataset_id, query_from_fixture(G1["queries"][2])
    )
    old = batch_rows()[1]
    observed = replace(old, occurrences=(Occurrence("REOBS", datetime(2026, 4, 1, tzinfo=UTC)),))
    store.import_batch(db.importer, db.dataset_id, "REOBS", (observed,))
    assert (
        db.reader.execute(
            "SELECT count(*) FROM chronicle.revision WHERE dataset_id=%s", (db.dataset_id,)
        ).fetchone()[0]
        == 3
    )
    assert store.select_context(db.reader, old_context) == G1["queries"][2]["expected"]

    # A valid new earlier month inserts first, then a conflicting January row aborts
    # both the insertion and its batch header. Nothing partial becomes accepted.
    new = replace(
        old,
        observation_month=date(2025, 11, 1),
        occurrences=(Occurrence("BAD", datetime(2026, 4, 2, tzinfo=UTC)),),
    )
    conflict = replace(new, observation_month=old.observation_month, value="999")
    with pytest.raises(ContractError) as caught:
        store.import_batch(db.importer, db.dataset_id, "BAD", (new, conflict))
    assert caught.value.code == "REVISION_CONFLICT"
    assert (
        db.reader.execute(
            "SELECT count(*) FROM chronicle.import_batch WHERE dataset_id=%s AND batch_id='BAD'",
            (db.dataset_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        db.reader.execute(
            "SELECT count(*) FROM chronicle.revision WHERE dataset_id=%s", (db.dataset_id,)
        ).fetchone()[0]
        == 3
    )


def test_equal_source_order_and_changed_release_evidence_reject(db):
    store.import_batch(db.importer, db.dataset_id, "B1", batch_rows())
    later = replace(
        batch_rows()[1],
        source_revision_key="ambiguous",
        value="103",
        occurrences=(Occurrence("BAD", datetime(2026, 4, 1, tzinfo=UTC)),),
    )
    with pytest.raises(ContractError, match="AMBIGUOUS_SOURCE_ORDER"):
        store.import_batch(db.importer, db.dataset_id, "BAD", (later,))
    with pytest.raises(ContractError) as caught:
        store.import_batch(
            db.importer,
            db.dataset_id,
            "BAD",
            (replace(later, source_revision_key="initial", source_order=99),),
        )
    assert caught.value.code == "RELEASE_CONFLICT"


def test_publication_retry_cannot_rewind_pointer_or_change_snapshot_members(db):
    import_fixture(db, G1)
    digest = db.reader.execute(
        "SELECT fixture_manifest_sha256 FROM chronicle.snapshot "
        "WHERE dataset_id=%s AND snapshot_id='S1'",
        (db.dataset_id,),
    ).fetchone()[0]
    assert store.publish_snapshot(db.publisher, db.dataset_id, "S1", ["B1"], digest, None) == "S1"
    assert current(db) == "S2"
    for batches, new_digest in ((["B1", "B2"], digest), (["B1"], "0" * 64)):
        with pytest.raises(ContractError, match="SNAPSHOT_IDENTITY_CONFLICT"):
            store.publish_snapshot(db.publisher, db.dataset_id, "S1", batches, new_digest, "S2")
    with pytest.raises(ContractError, match="SNAPSHOT_BASE_CHANGED"):
        store.publish_snapshot(db.publisher, db.dataset_id, "STALE", ["B1"], digest, "S1")
    assert current(db) == "S2"
    assert (
        db.reader.execute(
            "SELECT count(*) FROM chronicle.snapshot WHERE dataset_id=%s", (db.dataset_id,)
        ).fetchone()[0]
        == 2
    )


def test_context_pins_current_once_and_retry_reuses_original_pin(db):
    store.import_batch(db.importer, db.dataset_id, "B1", batch_rows())
    store.import_batch(db.importer, db.dataset_id, "B2", batch_rows("B2"))
    store.publish_snapshot(db.publisher, db.dataset_id, "S1", ["B1"], "a" * 64, None)
    request = uuid4()
    arguments = (
        db.reader,
        db.dataset_id,
        "latest_corrected",
        "synthetic",
        "employment",
        (date(2026, 1, 1),),
    )
    context = store.resolve_current_context(*arguments, context_id=request)
    assert store.select_context(db.reader, context)[0]["value"] == "100"
    store.publish_snapshot(db.publisher, db.dataset_id, "S2", ["B1", "B2"], "b" * 64, "S1")
    assert store.resolve_current_context(*arguments, context_id=request) == context
    assert store.select_context(db.reader, context)[0]["value"] == "100"
    latest = store.resolve_current_context(*arguments)
    assert store.select_context(db.reader, latest)[0]["value"] == "103"
    changed = Query("latest_corrected", "S2", "synthetic", "employment", (date(2026, 1, 1),))
    with pytest.raises(ContractError, match="CONTEXT_IDENTITY_CONFLICT"):
        store.create_context(db.reader, db.dataset_id, changed, context_id=request)


def test_concurrent_identical_batch_claims_have_one_set_of_occurrences(db):
    barrier = Barrier(2)
    first, second = db.connect("importer"), db.connect("importer")

    def run(conn):
        barrier.wait(timeout=5)
        return store.import_batch(conn, db.dataset_id, "B1", batch_rows())

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, conn) for conn in (first, second)]
        assert [future.result(timeout=10) for future in futures] == ["B1", "B1"]
    assert (
        db.reader.execute(
            "SELECT count(*) FROM chronicle.revision_occurrence WHERE dataset_id=%s",
            (db.dataset_id,),
        ).fetchone()[0]
        == 2
    )


def test_concurrent_publication_from_same_parent_has_one_winner(db):
    store.import_batch(db.importer, db.dataset_id, "B1", batch_rows())
    barrier = Barrier(2)
    first, second = db.connect("publisher"), db.connect("publisher")

    def run(conn, name):
        barrier.wait(timeout=5)
        try:
            return store.publish_snapshot(conn, db.dataset_id, name, ["B1"], "a" * 64, None)
        except ContractError as exc:
            assert exc.code == "SNAPSHOT_BASE_CHANGED"
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, first, "LEFT"), pool.submit(run, second, "RIGHT")]
        outcomes = [future.result(timeout=10) for future in futures]
    assert outcomes.count("rejected") == 1
    assert current(db) == next(value for value in outcomes if value != "rejected")
    assert (
        db.reader.execute(
            "SELECT count(*) FROM chronicle.snapshot WHERE dataset_id=%s", (db.dataset_id,)
        ).fetchone()[0]
        == 1
    )


def test_seal_waits_for_inflight_occurrence_then_prevents_late_append(db):
    store.import_batch(db.importer, db.dataset_id, "B1", batch_rows())
    db.importer.execute(
        "INSERT INTO chronicle.import_batch(dataset_id,batch_id,content_sha256,"
        "expected_occurrence_count) VALUES (%s,'OPEN',%s,1)",
        (db.dataset_id, "a" * 64),
    )
    writer, sealer = db.connect("importer"), db.connect("importer")
    started = Event()
    insert = (
        "INSERT INTO chronicle.revision_occurrence "
        "(dataset_id,batch_id,source_namespace,series_key,observation_month,"
        "source_revision_key,received_at) "
        "VALUES (%s,'OPEN','synthetic','employment','2026-01-01','initial','2026-04-01T00:00:00Z')"
    )

    def seal():
        started.set()
        sealer.execute("SELECT chronicle.seal_batch(%s,'OPEN')", (db.dataset_id,))

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with writer.transaction():
                writer.execute(insert, (db.dataset_id,))
                future = pool.submit(seal)
                assert started.wait(timeout=3)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    blockers = db.admin.execute(
                        "SELECT pg_blocking_pids(%s)", (sealer.info.backend_pid,)
                    ).fetchone()[0]
                    if writer.info.backend_pid in blockers:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("seal did not wait on the in-flight occurrence's batch lock")
                assert not future.done()
            future.result(timeout=5)
        finally:
            writer.rollback()
    assert (
        db.reader.execute(
            "SELECT state FROM chronicle.import_batch WHERE dataset_id=%s AND batch_id='OPEN'",
            (db.dataset_id,),
        ).fetchone()[0]
        == "validated"
    )
    with pytest.raises(errors.ObjectNotInPrerequisiteState, match="BATCH_SEALED"):
        writer.execute(insert.replace("2026-04-01", "2026-04-02"), (db.dataset_id,))


def test_role_setup_rejects_existing_membership_that_would_bypass_privileges(db):
    with db.admin.transaction(force_rollback=True):
        db.admin.execute("GRANT chronicle_owner TO chronicle_reader")
        with pytest.raises(RuntimeError, match="Unsafe role membership"):
            connections.provision_roles(db.admin)


def test_concurrent_same_context_retry_uses_winners_pin_after_pointer_advances(db):
    store.import_batch(db.importer, db.dataset_id, "B1", batch_rows())
    store.import_batch(db.importer, db.dataset_id, "B2", batch_rows("B2"))
    store.publish_snapshot(db.publisher, db.dataset_id, "S1", ["B1"], "a" * 64, None)
    first, second = db.connect("reader"), db.connect("reader")
    request = uuid4()
    arguments = (db.dataset_id, "latest_corrected", "synthetic", "employment", (date(2026, 1, 1),))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with first.transaction():
            winner = store.resolve_current_context(first, *arguments, context_id=request)
            store.publish_snapshot(db.publisher, db.dataset_id, "S2", ["B1", "B2"], "b" * 64, "S1")
            pending = pool.submit(
                store.resolve_current_context, second, *arguments, context_id=request
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                blockers = db.admin.execute(
                    "SELECT pg_blocking_pids(%s)", (second.info.backend_pid,)
                ).fetchone()[0]
                if first.info.backend_pid in blockers:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("same-context retry did not wait for the first context transaction")
            assert not pending.done()
        assert pending.result(timeout=5) == winner
    assert current(db) == "S2"
    assert store.select_context(db.reader, winner)[0]["value"] == "100"
