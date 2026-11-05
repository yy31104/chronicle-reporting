"""HTTP jobs hold a visible committed pin until their actual backend has stopped."""

import json
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from chronicle import api
from chronicle.access import configure_access
from chronicle.demo import seed


def test_http_cancel_releases_pin_only_after_real_query_termination(db, tmp_path, monkeypatch):
    seed(connect=db.connect, dataset_id=db.dataset_id)
    policy, credentials = configure_access(tmp_path / "access", [db.dataset_id])
    token = json.loads(credentials.read_text())["viewer"]
    headers = {"Authorization": f"Bearer {token}"}
    prefix = f"/v1/datasets/{db.dataset_id}"
    started = threading.Event()
    backend = []
    build = api.build_report

    def blocked_report(conn, *args, **kwargs):
        # This writes a real candidate report inside the worker transaction. It
        # must remain invisible and roll back when the subsequent query is cancelled.
        document = build(conn, *args, **kwargs)
        backend.append(conn.info.backend_pid)
        started.set()
        conn.execute("SELECT pg_sleep(12)")
        return document

    monkeypatch.setattr(api, "build_report", blocked_report)
    with TestClient(api.create_app(access_path=policy, connect=db.connect)) as client:
        context_id, report_id = str(uuid4()), str(uuid4())
        context = client.post(
            prefix + "/contexts",
            headers=headers,
            json={
                "context_id": context_id,
                "snapshot_id": "S1",
                "mode": "system_snapshot",
                "source_namespace": "synthetic",
                "series_key": "employment",
                "months": ["2026-01-01"],
                "expected_unit": {"name": "thousand_persons", "metadata_version": "v1"},
            },
        )
        assert context.status_code == 200, context.text
        response = client.post(
            prefix + "/jobs",
            headers=headers,
            json={"context_id": context_id, "report_id": report_id, "formula": "employment_delta"},
        )
        assert response.status_code == 202, response.text
        job_url = prefix + "/jobs/" + response.json()["job_id"]
        assert started.wait(5)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            waiting = db.admin.execute(
                "SELECT wait_event FROM pg_stat_activity WHERE pid=%s", (backend[0],)
            ).fetchone()
            if waiting == ("PgSleep",):
                break
            time.sleep(0.01)
        else:
            raise AssertionError("real backend never entered pg_sleep")

        # A different connection must see the pin while the report transaction is
        # still uncommitted. A pin in the worker's own transaction fails this check.
        pins = db.reader.execute(
            "SELECT pin_id,snapshot_id,released_at FROM chronicle.reader_pin WHERE dataset_id=%s",
            (db.dataset_id,),
        ).fetchall()
        assert len(pins) == 1
        pin_id, snapshot_id, released_at = pins[0]
        assert snapshot_id == "S1"
        assert released_at is None
        assert (
            db.reader.execute(
                "SELECT 1 FROM chronicle.report_run WHERE report_id=%s", (report_id,)
            ).fetchone()
            is None
        )
        running = client.get(job_url, headers=headers).json()
        assert running["state"] == "running"
        assert running["execution_stopped"] is False

        cancelled = client.delete(job_url, headers=headers)
        assert cancelled.status_code == 202
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = client.get(job_url, headers=headers).json()
            if result["execution_stopped"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("cancelled worker did not finish")
        assert result["state"] == "cancelled"
        assert result["result"] is None
        assert (
            db.admin.execute(
                "SELECT 1 FROM pg_stat_activity WHERE pid=%s", (backend[0],)
            ).fetchone()
            is None
        )
        assert db.reader.execute(
            "SELECT released_at IS NOT NULL FROM chronicle.reader_pin WHERE pin_id=%s", (pin_id,)
        ).fetchone() == (True,)
        assert (
            db.reader.execute(
                "SELECT 1 FROM chronicle.report_run WHERE report_id=%s", (report_id,)
            ).fetchone()
            is None
        )
