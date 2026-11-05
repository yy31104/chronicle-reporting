"""Real-role, filesystem, publication and process-crash artifact checks."""

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from chronicle import objects, store
from chronicle.domain import Occurrence
from chronicle.errors import ContractError
from tests.postgres.conftest import database as database_fixture
from tests.support import revision_from_fixture

G1 = json.loads((Path(__file__).parents[1] / "fixtures/golden/G1.json").read_text())
POLICY = {
    "policy_id": "synthetic-test",
    "version": "1",
    "decision_state": "approved_scoped",
    "permitted_uses": ["local_ingest", "local_analysis"],
    "scope": "Synthetic test bytes",
}


@pytest.fixture
def database():
    # Objects are global by digest: isolate the entire catalog per test, not just
    # the dataset, so each temporary root is a truthful database/object pair.
    yield from database_fixture.__wrapped__()


def prepare(db, root, batch="B1", raw=b"synthetic archive", *, policy=None):
    row = revision_from_fixture(G1["revisions"][0])
    row = replace(row, occurrences=(Occurrence(batch, datetime(2026, 10, 29, tzinfo=UTC)),))
    store.import_batch(db.importer, db.dataset_id, batch, (row,))
    staged = objects.stage_bytes(
        db.publisher,
        db.dataset_id,
        raw,
        policy or {**POLICY, "policy_id": db.dataset_id},
        {"synthetic": True},
        root=root,
    )
    objects.attach_batch_object(db.publisher, db.dataset_id, batch, staged)
    return staged


def publish(db, root, snapshot="S1", batches=("B1",), parent=None, request=None, fault=None):
    return objects.publish_snapshot(
        db.publisher,
        db.dataset_id,
        snapshot,
        batches,
        parent,
        request or uuid4(),
        root=root,
        _fault=fault,
    )


def test_durable_closure_cumulative_membership_and_replay_after_pointer_move(db, tmp_path):
    first = prepare(db, tmp_path)
    request = uuid4()
    assert publish(db, tmp_path, request=request) == "S1"
    objects.end_ticket(db.publisher, first)
    second = prepare(db, tmp_path, "B2", b"second archive")
    assert publish(db, tmp_path, "S2", ("B2",), "S1") == "S2"
    objects.end_ticket(db.publisher, second)
    assert publish(db, tmp_path, request=request) == "S1"
    assert db.reader.execute(
        "SELECT current_snapshot_id FROM chronicle.dataset WHERE dataset_id=%s", (db.dataset_id,)
    ).fetchone() == ("S2",)
    assert db.reader.execute(
        "SELECT batch_id FROM chronicle.snapshot_batch WHERE dataset_id=%s "
        "AND snapshot_id='S2' ORDER BY batch_id",
        (db.dataset_id,),
    ).fetchall() == [("B1",), ("B2",)]
    assert objects.verify_closure(db.reader, root=tmp_path) == []
    assert (
        objects.read_object(db.reader, db.dataset_id, "S1", first.sha256, root=tmp_path)
        == b"synthetic archive"
    )
    with pytest.raises(ContractError, match="outside"):
        objects.read_object(db.reader, db.dataset_id, "S1", second.sha256, root=tmp_path)
    assert objects.collect_abandoned(db.publisher, grace_seconds=0, root=tmp_path) == []
    with pytest.raises(ContractError) as caught:
        publish(db, tmp_path, "DIFFERENT", request=request)
    assert caught.value.code == "PUBLICATION_IDENTITY_CONFLICT"


def test_missing_corrupt_and_symlink_objects_block_access(db, tmp_path):
    staged = prepare(db, tmp_path)
    publish(db, tmp_path)
    path = objects.object_path(staged.sha256, tmp_path)
    original = path.read_bytes()
    path.write_bytes(b"corruption")
    assert objects.verify_closure(db.reader, root=tmp_path) == [
        {"object_sha256": staged.sha256, "code": "SNAPSHOT_EVIDENCE_UNAVAILABLE"}
    ]
    with pytest.raises(ContractError):
        objects.require_snapshot_access(db.reader, db.dataset_id, "S1", root=tmp_path)
    path.unlink()
    external = tmp_path / "untrusted"
    external.write_bytes(original)
    path.symlink_to(external)
    with pytest.raises(ContractError):
        objects.read_object(db.reader, db.dataset_id, "S1", staged.sha256, root=tmp_path)
    path.unlink()
    with pytest.raises(ContractError):
        objects.read_object(db.reader, db.dataset_id, "S1", staged.sha256, root=tmp_path)


def test_live_rights_revocation_and_forbidden_export(db, tmp_path):
    staged = prepare(db, tmp_path)
    publish(db, tmp_path)
    with pytest.raises(ContractError) as caught:
        objects.read_object(db.reader, db.dataset_id, "S1", staged.sha256, "export", root=tmp_path)
    assert caught.value.code == "RIGHTS_DENIED"
    objects.revoke_rights(db.publisher, staged.policy_sha256, "Test revocation")
    with pytest.raises(ContractError) as caught:
        objects.require_snapshot_access(db.reader, db.dataset_id, "S1", root=tmp_path)
    assert caught.value.code == "RIGHTS_DENIED"
    assert objects.verify_closure(db.reader, root=tmp_path) == []
    assert objects.collect_abandoned(db.publisher, grace_seconds=0, root=tmp_path) == []


def test_revoked_or_unapproved_policy_cannot_stage_or_publish(db, tmp_path):
    with pytest.raises(ContractError) as caught:
        objects.stage_bytes(
            db.publisher,
            db.dataset_id,
            b"x",
            {**POLICY, "decision_state": "pending"},
            {},
            root=tmp_path,
        )
    assert caught.value.code == "RIGHTS_DENIED"
    staged = prepare(db, tmp_path)
    objects.revoke_rights(db.publisher, staged.policy_sha256, "Test revocation")
    with pytest.raises(ContractError) as caught:
        publish(db, tmp_path)
    assert caught.value.code == "RIGHTS_DENIED"
    assert db.reader.execute(
        "SELECT current_snapshot_id FROM chronicle.dataset WHERE dataset_id=%s", (db.dataset_id,)
    ).fetchone() == (None,)


def test_legacy_publication_cannot_bypass_managed_object_closure(db, tmp_path):
    prepare(db, tmp_path)
    with pytest.raises(ContractError) as caught:
        store.publish_snapshot(db.publisher, db.dataset_id, "bypass", ["B1"], "a" * 64, None)
    assert caught.value.code == "ARTIFACT_PUBLICATION_REQUIRED"
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.publisher.execute(
            "SELECT chronicle.publish_snapshot_legacy(%s,'bypass',ARRAY['B1'],%s,NULL)",
            (db.dataset_id, "a" * 64),
        )


def test_same_hash_concurrent_writers_adopt_without_overwrite(db, tmp_path):
    raw = b"same content" * 100

    def run():
        conn = db.connect("publisher")
        return objects.stage_bytes(conn, db.dataset_id, raw, POLICY, {}, root=tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        staged = list(pool.map(lambda _: run(), range(2)))
    assert staged[0].sha256 == staged[1].sha256
    path = objects.object_path(staged[0].sha256, tmp_path)
    before = path.stat().st_ino
    again = run()
    assert path.stat().st_ino == before
    assert path.read_bytes() == raw
    assert db.reader.execute(
        "SELECT count(*) FROM chronicle.ticket_object WHERE object_sha256=%s", (again.sha256,)
    ).fetchone() == (3,)
    assert objects.collect_abandoned(db.publisher, grace_seconds=0, root=tmp_path) == []


@pytest.mark.parametrize("phase", ["temporary_durable", "registered", "promoted", "ready"])
def test_stage_failure_never_publishes_and_active_ticket_prevents_collection(db, tmp_path, phase):
    def fault(actual):
        if actual == phase:
            raise RuntimeError("injected crash boundary")

    worker, ticket = uuid4(), uuid4()
    raw = b"boundary:" + phase.encode()
    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(RuntimeError, match="injected"):
        objects.stage_bytes(
            db.publisher,
            db.dataset_id,
            raw,
            POLICY,
            {},
            root=tmp_path,
            ticket_id=ticket,
            worker_id=worker,
            _fault=fault,
        )
    assert db.reader.execute(
        "SELECT current_snapshot_id FROM chronicle.dataset WHERE dataset_id=%s", (db.dataset_id,)
    ).fetchone() == (None,)
    assert objects.collect_abandoned(db.publisher, grace_seconds=0, root=tmp_path) == []
    policy_hash = db.reader.execute(
        "SELECT policy_sha256 FROM chronicle.staging_ticket WHERE ticket_id=%s", (ticket,)
    ).fetchone()[0]
    objects.end_ticket(
        db.publisher, objects.StagedObject(ticket, worker, digest, policy_hash), abandoned=True
    )
    removed = objects.collect_abandoned(db.publisher, grace_seconds=0, root=tmp_path)
    assert removed == ([] if phase == "temporary_durable" else [digest])
    assert not objects.object_path(digest, tmp_path).exists()


@pytest.mark.parametrize("phase", ["manifest_ready", "before_commit", "after_commit"])
def test_publication_crash_boundary_has_complete_or_no_snapshot(db, tmp_path, phase):
    prepare(db, tmp_path)
    request = uuid4()

    def fault(actual):
        if actual == phase:
            raise RuntimeError("injected publication failure")

    with pytest.raises(RuntimeError, match="injected"):
        publish(db, tmp_path, request=request, fault=fault)
    pointer = db.reader.execute(
        "SELECT current_snapshot_id FROM chronicle.dataset WHERE dataset_id=%s", (db.dataset_id,)
    ).fetchone()[0]
    assert pointer == ("S1" if phase == "after_commit" else None)
    assert objects.verify_closure(db.reader, root=tmp_path) == []
    assert publish(db, tmp_path, request=request) == "S1"


@pytest.mark.parametrize("phase", ["deleting", "unlinked"])
def test_collection_crash_is_restartable_and_retains_tombstone(db, tmp_path, phase):
    staged = objects.stage_bytes(
        db.publisher, db.dataset_id, b"unpublished", POLICY, {}, root=tmp_path
    )
    objects.end_ticket(db.publisher, staged, abandoned=True)

    def fault(actual):
        if actual == phase:
            raise RuntimeError("injected deletion failure")

    with pytest.raises(RuntimeError):
        objects.collect_abandoned(db.publisher, grace_seconds=0, root=tmp_path, _fault=fault)
    assert db.reader.execute(
        "SELECT state FROM chronicle.storage_object WHERE object_sha256=%s", (staged.sha256,)
    ).fetchone() == ("deleting",)
    assert objects.collect_abandoned(db.publisher, grace_seconds=0, root=tmp_path) == [
        staged.sha256
    ]
    assert db.reader.execute(
        "SELECT state FROM chronicle.storage_object WHERE object_sha256=%s", (staged.sha256,)
    ).fetchone() == ("deleted",)
    restaged = objects.stage_bytes(
        db.publisher, db.dataset_id, b"unpublished", POLICY, {}, root=tmp_path
    )
    assert restaged.sha256 == staged.sha256
    assert objects.object_path(staged.sha256, tmp_path).read_bytes() == b"unpublished"


def test_actual_worker_process_death_after_promotion_keeps_ticket_and_old_pointer(db, tmp_path):
    worker, ticket = uuid4(), uuid4()
    code = """import json,os,sys
from pathlib import Path
from uuid import UUID
from chronicle import db,objects
with db.connect('publisher',database=sys.argv[1]) as conn:
 objects.stage_bytes(conn,sys.argv[2],b'process crash bytes',json.loads(sys.argv[6]),{},
  root=Path(sys.argv[3]),worker_id=UUID(sys.argv[4]),ticket_id=UUID(sys.argv[5]),
  _fault=lambda phase: os._exit(23) if phase=='promoted' else None)
"""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            db.database,
            db.dataset_id,
            str(tmp_path),
            str(worker),
            str(ticket),
            json.dumps(POLICY),
        ],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        timeout=15,
    )
    assert proc.returncode == 23, proc.stderr.decode()
    digest = hashlib.sha256(b"process crash bytes").hexdigest()
    assert objects.object_path(digest, tmp_path).read_bytes() == b"process crash bytes"
    assert db.reader.execute(
        "SELECT state FROM chronicle.storage_object WHERE object_sha256=%s", (digest,)
    ).fetchone() == ("staged",)
    assert objects.collect_abandoned(db.publisher, grace_seconds=0, root=tmp_path) == []
    # Subprocess wait established actual termination; elapsed time alone did not.
    policy_hash = db.reader.execute(
        "SELECT policy_sha256 FROM chronicle.staging_ticket WHERE ticket_id=%s", (ticket,)
    ).fetchone()[0]
    objects.end_ticket(
        db.publisher, objects.StagedObject(ticket, worker, digest, policy_hash), abandoned=True
    )
    assert objects.collect_abandoned(db.publisher, grace_seconds=0, root=tmp_path) == [digest]


def test_reader_pin_releases_only_on_scope_exit_and_does_not_expire(db, tmp_path):
    prepare(db, tmp_path)
    publish(db, tmp_path)
    with objects.snapshot_pin(db.reader, db.dataset_id, "S1", root=tmp_path) as pin:
        assert db.reader.execute(
            "SELECT released_at FROM chronicle.reader_pin WHERE pin_id=%s", (pin,)
        ).fetchone() == (None,)
    assert db.reader.execute(
        "SELECT released_at IS NOT NULL FROM chronicle.reader_pin WHERE pin_id=%s", (pin,)
    ).fetchone() == (True,)


def test_no_runtime_direct_artifact_mutation_or_reader_publication(db):
    for statement in [
        "DELETE FROM chronicle.snapshot_object",
        "UPDATE chronicle.storage_object SET state='ready'",
        "INSERT INTO chronicle.rights_revocation VALUES ('a','reason',now())",
    ]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.publisher.execute(statement)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.reader.execute("SELECT chronicle.begin_object_deletion(%s,0)", ("a" * 64,))


def test_paused_promoted_writer_is_not_collected(db, tmp_path):
    from threading import Event
    from time import monotonic, sleep

    promoted, resume = Event(), Event()
    writer = db.connect("publisher")
    collector = db.connect("publisher")

    def pause(phase):
        if phase == "promoted":
            promoted.set()
            assert resume.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(
            objects.stage_bytes,
            writer,
            db.dataset_id,
            b"paused writer",
            POLICY,
            {},
            root=tmp_path,
            _fault=pause,
        )
        assert promoted.wait(5)
        collecting = pool.submit(
            objects.collect_abandoned, collector, grace_seconds=0, root=tmp_path
        )
        deadline = monotonic() + 4
        try:
            while monotonic() < deadline:
                blockers = db.admin.execute(
                    "SELECT pg_blocking_pids(%s)", (collector.info.backend_pid,)
                ).fetchone()[0]
                if writer.info.backend_pid in blockers:
                    break
                sleep(0.01)
            else:
                pytest.fail("collector did not wait on the real writer object lock")
            assert not collecting.done()
        finally:
            resume.set()
        staged = writing.result(timeout=5)
        assert collecting.result(timeout=5) == []
    assert objects.object_path(staged.sha256, tmp_path).read_bytes() == b"paused writer"


def test_concurrent_publishers_require_explicit_rebase(db, tmp_path):
    prepare(db, tmp_path)
    publish(db, tmp_path)
    prepare(db, tmp_path, "B2", b"candidate-two")
    prepare(db, tmp_path, "B3", b"candidate-three")

    def run(number):
        conn = db.connect("publisher")
        try:
            return objects.publish_snapshot(
                conn, db.dataset_id, f"S{number}", [f"B{number}"], "S1", uuid4(), root=tmp_path
            )
        except ContractError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [2, 3]))
    assert results.count("SNAPSHOT_BASE_CHANGED") == 1
    winner = next(result for result in results if result != "SNAPSHOT_BASE_CHANGED")
    assert objects.verify_closure(db.reader, root=tmp_path) == []
    assert db.reader.execute(
        "SELECT batch_id FROM chronicle.snapshot_batch WHERE dataset_id=%s "
        "AND snapshot_id=%s ORDER BY batch_id",
        (db.dataset_id, winner),
    ).fetchall() == [("B1",), ("B" + winner[1:],)]


def test_published_batch_cannot_gain_additional_artifacts(db, tmp_path):
    original = prepare(db, tmp_path)
    publish(db, tmp_path)
    extra = objects.stage_bytes(
        db.publisher, db.dataset_id, b"extra evidence", POLICY, {}, root=tmp_path
    )
    with pytest.raises(ContractError) as caught:
        objects.attach_batch_object(db.publisher, db.dataset_id, "B1", extra)
    assert caught.value.code == "BATCH_ARTIFACT_CLOSED"
    objects.attach_batch_object(db.publisher, db.dataset_id, "B1", original)
    assert (
        len(
            db.reader.execute(
                "SELECT object_sha256 FROM chronicle.batch_artifact "
                "WHERE dataset_id=%s AND batch_id='B1'",
                (db.dataset_id,),
            ).fetchall()
        )
        == 1
    )


def test_manifest_must_match_exact_batch_and_object_closure(db, tmp_path):
    prepare(db, tmp_path)
    manifest = db.publisher.execute(
        "SELECT chronicle.artifact_manifest(%s,'S1',NULL,ARRAY['B1'])", (db.dataset_id,)
    ).fetchone()[0]
    manifest["artifacts"] = []
    staged = objects.stage_bytes(
        db.publisher, db.dataset_id, objects.canonical_bytes(manifest), POLICY, {}, root=tmp_path
    )
    with pytest.raises(psycopg.Error) as caught:
        db.publisher.execute(
            "SELECT chronicle.publish_artifact_snapshot(%s,%s,'S1',NULL,ARRAY['B1'],%s,%s,%s,%s)",
            (
                uuid4(),
                db.dataset_id,
                objects.canonical_bytes(manifest).decode(),
                staged.ticket_id,
                staged.worker_id,
                staged.sha256,
            ),
        )
    assert caught.value.diag.constraint_name == "object_manifest_mismatch"
    assert db.reader.execute(
        "SELECT current_snapshot_id FROM chronicle.dataset WHERE dataset_id=%s", (db.dataset_id,)
    ).fetchone() == (None,)


def test_owner_cannot_mutate_policy_or_permanent_reference(db, tmp_path):
    staged = prepare(db, tmp_path)
    publish(db, tmp_path)
    for statement in [
        "UPDATE chronicle.rights_policy SET policy=policy",
        "UPDATE chronicle.snapshot_object SET object_sha256=object_sha256",
        "DELETE FROM chronicle.artifact_publication",
        "TRUNCATE chronicle.rights_revocation",
    ]:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            with db.admin.transaction():
                db.admin.execute("SET LOCAL ROLE chronicle_owner")
                db.admin.execute(statement)
    assert objects.read_staged_object(db.reader, staged, root=tmp_path) == b"synthetic archive"
    objects.revoke_rights(db.publisher, staged.policy_sha256, "test")
    with pytest.raises(ContractError) as caught:
        objects.read_staged_object(db.reader, staged, root=tmp_path)
    assert caught.value.code == "RIGHTS_DENIED"
