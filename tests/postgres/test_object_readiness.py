"""Request checks stay cheap; explicit audits/readouts detect actual byte corruption."""

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chronicle import api, objects, store
from chronicle.access import configure_access
from chronicle.errors import ContractError
from tests.postgres.conftest import database as database_fixture
from tests.support import revision_from_fixture


@pytest.fixture
def database():
    # Pair this catalog with one object root; digests are global across datasets.
    yield from database_fixture.__wrapped__()


@pytest.fixture
def managed_http(db, tmp_path, monkeypatch):
    raw_fixture = json.loads((Path(__file__).parents[1] / "fixtures/golden/G1.json").read_text())
    rows = tuple(revision_from_fixture(row) for row in raw_fixture["revisions"][:2])
    store.import_batch(db.importer, db.dataset_id, "B1", rows)
    root = tmp_path / "objects"
    monkeypatch.setenv("CHRONICLE_OBJECT_ROOT", str(root))
    staged = []
    for index in range(3):
        item = objects.stage_bytes(
            db.publisher,
            db.dataset_id,
            f"archive-{index}".encode(),
            {
                "decision_state": "approved_scoped",
                "permitted_uses": ["local_ingest", "local_analysis"],
            },
            {"synthetic": True, "index": index},
            root=root,
        )
        objects.attach_batch_object(db.publisher, db.dataset_id, "B1", item)
        staged.append(item)
    objects.publish_snapshot(db.publisher, db.dataset_id, "S1", ["B1"], None, uuid4(), root=root)
    for item in staged:
        objects.end_ticket(db.publisher, item)
    policy, credentials = configure_access(tmp_path / "access", [db.dataset_id])
    headers = {"Authorization": "Bearer " + json.loads(credentials.read_text())["viewer"]}
    with TestClient(api.create_app(access_path=policy, connect=db.connect)) as client:
        yield client, headers, root, staged, policy


def context_body():
    return {
        "context_id": str(uuid4()),
        "snapshot_id": "S1",
        "mode": "system_snapshot",
        "source_namespace": "synthetic",
        "series_key": "employment",
        "months": ["2026-01-01"],
    }


def test_ready_context_report_and_replay_do_not_read_object_contents(db, managed_http, monkeypatch):
    client, headers, root, staged, _ = managed_http
    hashes = db.reader.execute("SELECT object_sha256 FROM chronicle.snapshot_object").fetchall()
    assert len(hashes) == 4  # Three source objects plus the complete manifest.
    watched = {
        (info.st_dev, info.st_ino)
        for (digest,) in hashes
        for info in [objects.object_path(digest, root).stat()]
    }
    reads, hash_calls = [], []
    original_read, original_hash = os.read, objects._hash_object

    def counted_read(fd, size):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) in watched:
            reads.append(size)
        return original_read(fd, size)

    def counted_hash(*args, **kwargs):
        hash_calls.append(args[0])
        return original_hash(*args, **kwargs)

    monkeypatch.setattr(os, "read", counted_read)
    monkeypatch.setattr(objects, "_hash_object", counted_hash)
    prefix = f"/v1/datasets/{db.dataset_id}"
    assert client.get("/health/ready").status_code == 200
    body = context_body()
    assert client.post(prefix + "/contexts", headers=headers, json=body).status_code == 200
    report_id = str(uuid4())
    report = client.post(
        prefix + "/reports",
        headers=headers,
        json={
            "context_id": body["context_id"],
            "report_id": report_id,
            "formula": "employment_delta",
        },
    )
    assert report.status_code == 200, report.text
    assert report.json()["rows"][0]["value"] == "10.000000000000"
    assert client.get(prefix + "/reports/" + report_id, headers=headers).status_code == 200
    assert reads == []
    assert hash_calls == []
    # The explicit byte-returning path does perform a full content verification.
    assert (
        objects.read_object(db.reader, db.dataset_id, "S1", staged[0].sha256, root=root)
        == b"archive-0"
    )
    assert hash_calls == [staged[0].sha256]
    assert reads


def test_equal_length_tamper_is_detected_by_read_and_audit_not_readiness(db, managed_http):
    client, headers, root, staged, _ = managed_http
    path = objects.object_path(staged[0].sha256, root)
    path.write_bytes(b"tampered!")  # Same nine-byte length as archive-0.
    objects.require_snapshot_access(db.reader, db.dataset_id, "S1", root=root)
    assert client.get("/health/ready").status_code == 200
    with pytest.raises(ContractError) as caught:
        objects.read_object(db.reader, db.dataset_id, "S1", staged[0].sha256, root=root)
    assert caught.value.code == "SNAPSHOT_EVIDENCE_UNAVAILABLE"
    assert objects.verify_closure(db.reader, root=root) == [
        {"object_sha256": staged[0].sha256, "code": "SNAPSHOT_EVIDENCE_UNAVAILABLE"}
    ]
    # Readiness is service/dependency readiness, not a per-snapshot data audit.
    path.unlink()
    assert client.get("/health/ready").status_code == 200
    response = client.post(
        f"/v1/datasets/{db.dataset_id}/contexts", headers=headers, json=context_body()
    )
    assert response.status_code == 503
    assert response.json()["code"] == "SNAPSHOT_EVIDENCE_UNAVAILABLE"


def test_metadata_checks_reject_symlink_ancestor_without_content_reads(db, managed_http, tmp_path):
    _, _, root, staged, _ = managed_http
    ancestor = tmp_path / "linked-parent"
    ancestor.symlink_to(root.parent, target_is_directory=True)
    with pytest.raises(ContractError) as caught:
        objects.require_snapshot_access(db.reader, db.dataset_id, "S1", root=ancestor / root.name)
    assert caught.value.code == "SNAPSHOT_EVIDENCE_UNAVAILABLE"
    assert objects.verify_closure(db.reader, root=root) == []
    prefix = objects.object_path(staged[0].sha256, root).parent
    moved = tmp_path / "saved-prefix"
    prefix.rename(moved)
    prefix.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ContractError):
        objects.require_snapshot_access(db.reader, db.dataset_id, "S1", root=root)


def test_explicit_hashing_reads_bounded_chunks_without_buffering_for_audit(
    db, tmp_path, monkeypatch
):
    payload = b"x" * (objects.HASH_CHUNK_BYTES * 2 + 17)
    digest = hashlib.sha256(payload).hexdigest()
    destination = objects.object_path(digest, tmp_path)
    destination.parent.mkdir()
    destination.write_bytes(payload)
    sizes = []
    original = os.read

    def counted_read(fd, size):
        sizes.append(size)
        return original(fd, size)

    monkeypatch.setattr(os, "read", counted_read)
    objects._hash_object(digest, len(payload), tmp_path)
    assert len(sizes) == 4  # Three chunks and an EOF check.
    assert set(sizes) == {1024 * 1024}
    assert objects._verified_bytes(digest, len(payload), tmp_path) == payload


def test_ready_remains_healthy_when_both_business_slots_have_blocked_reports(
    db, managed_http, monkeypatch
):
    client, headers, _, _, _ = managed_http
    prefix = f"/v1/datasets/{db.dataset_id}"
    requests = []
    for _ in range(2):
        context = context_body()
        assert client.post(prefix + "/contexts", headers=headers, json=context).status_code == 200
        requests.append(
            {
                "context_id": context["context_id"],
                "report_id": str(uuid4()),
                "formula": "employment_delta",
            }
        )
    key = int.from_bytes(uuid4().bytes[:8], "big", signed=True)
    db.admin.execute("SELECT pg_advisory_lock(%s)", (key,))
    lock, started = threading.Lock(), threading.Event()
    backends = []
    original = api.build_report

    def blocked(conn, *args, **kwargs):
        with lock:
            backends.append(conn.info.backend_pid)
            if len(backends) == 2:
                started.set()
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(api, "build_report", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(client.post, prefix + "/reports", headers=headers, json=body)
            for body in requests
        ]
        try:
            assert started.wait(3)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if all(
                    db.admin.info.backend_pid
                    in db.admin.execute("SELECT pg_blocking_pids(%s)", (pid,)).fetchone()[0]
                    for pid in backends
                ):
                    break
                time.sleep(0.01)
            else:
                pytest.fail("both report backends did not block on the actual database lock")
            busy = client.post(prefix + "/reports", headers=headers, json=requests[0])
            assert busy.status_code == 503
            assert busy.json()["code"] == "SERVICE_BUSY"
            ready = client.get("/health/ready")
            assert ready.status_code == 200, ready.text
            assert ready.json() == {"status": "ready"}
            assert all(not future.done() for future in futures)
        finally:
            db.admin.execute("SELECT pg_advisory_unlock(%s)", (key,))
        for future in futures:
            result = future.result(timeout=5)
            assert result.status_code == 200, result.text


def test_ready_fails_closed_for_policy_corruption(db, managed_http):
    client, _, _, _, policy = managed_http
    policy.write_text("{}")
    assert client.get("/health/ready").status_code == 503
