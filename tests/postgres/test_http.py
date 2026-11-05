"""HTTP authentication, resource bounds and real PostgreSQL report round trips."""

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chronicle.access import configure_access
from chronicle.api import create_app
from chronicle.demo import seed


@pytest.fixture
def http(db, tmp_path):
    seed(connect=db.connect, dataset_id=db.dataset_id)
    policy, credentials = configure_access(tmp_path / "private", [db.dataset_id])
    token = json.loads(credentials.read_text())["viewer"]
    with TestClient(
        create_app(access_path=policy, connect=db.connect), raise_server_exceptions=False
    ) as client:
        yield client, {"Authorization": f"Bearer {token}"}


def body():
    return {
        "context_id": str(uuid4()),
        "snapshot_id": "S1",
        "mode": "system_snapshot",
        "source_namespace": "synthetic",
        "series_key": "employment",
        "months": ["2026-01-01"],
        "expected_unit": {"name": "thousand_persons", "metadata_version": "v1"},
    }


def test_report_roundtrip_and_authz(db, http):
    client, headers = http
    url = f"/v1/datasets/{db.dataset_id}"
    assert client.get("/health/ready").status_code == 200
    assert client.get("/v1/datasets").status_code == 401
    assert client.get("/v1/datasets", headers={"Authorization": "Bearer wrong"}).status_code == 401
    data = body()
    assert client.post(url + "/contexts", json=data, headers=headers).status_code == 200
    result = client.post(
        url + "/reports",
        headers=headers,
        json={
            "context_id": data["context_id"],
            "report_id": str(uuid4()),
            "formula": "employment_delta",
        },
    )
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved["rows"][0]["value"] == "10.000000000000"
    assert client.get(url + "/reports/" + saved["report_id"], headers=headers).json() == saved
    evidence = client.get(url + "/contexts/" + data["context_id"] + "/evidence", headers=headers)
    assert evidence.status_code == 200
    assert evidence.json()["inputs"][0]["value"] == "100"
    denied = client.get("/v1/datasets/foreign/reports/" + saved["report_id"], headers=headers)
    assert denied.status_code == 403
    assert "100" not in denied.text
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["x-request-id"]


def test_input_bounds_and_safe_errors(db, http):
    client, headers = http
    url = f"/v1/datasets/{db.dataset_id}/contexts"
    for update in (
        {"months": ["2026-01-01"] * 121},
        {"months": ["2026-01-02"]},
        {"months": ["20260101"]},
        {"sql": "SELECT secret"},
        {"mode": "source_as_of"},
    ):
        response = client.post(url, json=body() | update, headers=headers)
        assert response.status_code == 422, response.text
        assert "SELECT secret" not in response.text
        assert "psycopg" not in response.text
    response = client.post(url, content=b"x" * 65537, headers=headers)
    assert response.status_code == 413
    invalid = client.post(url, json=body() | {"snapshot_id": "absent"}, headers=headers)
    assert invalid.status_code == 404, invalid.text
    assert "UNKNOWN_SNAPSHOT" in invalid.text
