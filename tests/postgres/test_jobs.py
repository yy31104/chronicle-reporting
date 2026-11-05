"""Cancellation must stop an actual backend and roll back, not just alter a status."""

import threading
import time
from uuid import uuid4

import pytest

from chronicle.errors import ContractError
from chronicle.jobs import JobManager


def test_backend_cancellation_rolls_back_and_releases_capacity(db):
    manager = JobManager(db.connect, workers=1)
    started = threading.Event()
    backend = []

    def action(conn):
        backend.append(conn.info.backend_pid)
        started.set()
        conn.execute("SELECT pg_sleep(12)")
        return {"wrong": "cancel must prevent this"}

    try:
        job = manager.submit(db.dataset_id, "viewer", uuid4(), action)
        assert started.wait(3)
        # Confirm PostgreSQL actually entered the sleep before cancelling.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            row = db.admin.execute(
                "SELECT wait_event FROM pg_stat_activity WHERE pid=%s", (backend[0],)
            ).fetchone()
            if row and row[0] == "PgSleep":
                break
            time.sleep(0.01)
        else:
            pytest.fail("backend did not enter the blocking query")
        with pytest.raises(ContractError) as full:
            manager.submit(db.dataset_id, "viewer", uuid4(), action)
        assert full.value.code == "SERVICE_BUSY"
        manager.cancel(job)
        assert job.done.wait(3)
        result = job.snapshot()
        assert result["state"] == "cancelled"
        assert result["execution_stopped"] is True
        assert result["result"] is None
        assert (
            db.admin.execute(
                "SELECT state FROM pg_stat_activity WHERE pid=%s", (backend[0],)
            ).fetchone()
            is None
        )
        replacement = manager.submit(db.dataset_id, "viewer", uuid4(), lambda c: {"ok": True})
        assert replacement.done.wait(3)
        assert replacement.snapshot()["state"] == "completed"
        with pytest.raises(ContractError):
            manager.get(job.job_id, db.dataset_id, "other-viewer")
    finally:
        manager.close()


def test_committed_job_remains_completed_when_pin_cleanup_fails(db):
    from contextlib import contextmanager

    @contextmanager
    def failed_cleanup():
        yield
        raise RuntimeError("cleanup connection lost")

    manager = JobManager(db.connect, workers=1)
    try:
        job = manager.submit(
            db.dataset_id, "viewer", uuid4(), lambda c: {"committed": True}, guard=failed_cleanup
        )
        assert job.done.wait(3)
        result = job.snapshot()
        assert result["state"] == "completed"
        assert result["result"] == {"report_id": job.report_id}
        assert result["error"]["code"] == "CLEANUP_INCOMPLETE"
    finally:
        manager.close()
