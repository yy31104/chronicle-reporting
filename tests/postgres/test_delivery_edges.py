"""Independent HTTP boundaries over actual scoped reports and live source rights."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chronicle import objects, store
from chronicle.access import configure_access
from chronicle.api import create_app
from chronicle.domain import Query
from chronicle.reports import build_report
from tests.postgres.conftest import database as database_fixture
from tests.postgres.conftest import import_fixture
from tests.support import revision_from_fixture

G1 = json.loads((Path(__file__).parents[1] / "fixtures/golden/G1.json").read_text())


@pytest.fixture
def database():
    # Rights tests add global object entries; keep each catalog paired with its root.
    yield from database_fixture.__wrapped__()


@pytest.fixture
def delivery(db, tmp_path):
    policy, credentials = configure_access(tmp_path / "private", [db.dataset_id])
    tokens = json.loads(credentials.read_text())
    with TestClient(
        create_app(access_path=policy, connect=db.connect), raise_server_exceptions=False
    ) as client:
        yield SimpleNamespace(
            client=client,
            policy=policy,
            headers={"Authorization": "Bearer " + tokens["viewer"]},
            root=f"/v1/datasets/{db.dataset_id}",
        )


def report_via_http(delivery, snapshot="S1", *, expected_unit=True):
    request = {
        "context_id": str(uuid4()),
        "snapshot_id": snapshot,
        "mode": "system_snapshot",
        "source_namespace": "synthetic",
        "series_key": "employment",
        "months": ["2026-01-01"],
        "expected_unit": {"name": "thousand_persons", "metadata_version": "v1"},
    }
    if not expected_unit:
        request["expected_unit"] = None
    response = delivery.client.post(
        delivery.root + "/contexts", json=request, headers=delivery.headers
    )
    assert response.status_code == 200, response.text
    response = delivery.client.post(
        delivery.root + "/reports",
        json={
            "context_id": request["context_id"],
            "report_id": str(uuid4()),
            "formula": "employment_delta",
        },
        headers=delivery.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_comparison_matrix_and_export_cannot_accept_foreign_inputs_in_either_position(db, delivery):
    import_fixture(db, G1)
    own = report_via_http(delivery)
    foreign = SimpleNamespace(**vars(db))
    foreign.dataset_id = "foreign-" + uuid4().hex
    store.create_dataset(db.importer, foreign.dataset_id)
    import_fixture(foreign, G1)
    from datetime import date

    foreign_context = store.create_context(
        db.reader,
        foreign.dataset_id,
        Query("system_snapshot", "S1", "synthetic", "employment", (date(2026, 1, 1),)),
    )
    other = build_report(db.reader, foreign_context, "employment_delta")
    # Even when the principal can access both datasets, the selected URL scope
    # must still bind every supplied context/report, not only the first operand.
    policy = json.loads(delivery.policy.read_text())
    policy["identities"][0]["datasets"].append(foreign.dataset_id)
    delivery.policy.write_text(json.dumps(policy))
    for left, right in ((own, other), (other, own)):
        for endpoint in ("comparisons", "comparison-export"):
            response = delivery.client.post(
                delivery.root + "/" + endpoint,
                json={
                    "old_report_id": left["report_id"],
                    "new_report_id": right["report_id"],
                },
                headers=delivery.headers,
            )
            assert response.status_code == 404, response.text
            assert response.json()["code"] == "REPORT_NOT_FOUND"
            assert "rows" not in response.json()
        response = delivery.client.post(
            delivery.root + "/matrix",
            json={
                "old_context_id": left["context_id"],
                "new_context_id": right["context_id"],
                "comparison_id": str(uuid4()),
                "formula": "employment_delta",
            },
            headers=delivery.headers,
        )
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "CONTEXT_NOT_FOUND"


def publish_rights_pair(db):
    stages = []
    parent = None
    for index, rows in enumerate((G1["revisions"][:2], G1["revisions"][2:]), 1):
        batch = f"B{index}"
        store.import_batch(
            db.importer, db.dataset_id, batch, tuple(revision_from_fixture(row) for row in rows)
        )
        uses = ["local_ingest", "local_analysis"] + (["public_export"] if index == 1 else [])
        staged = objects.stage_bytes(
            db.publisher,
            db.dataset_id,
            f"{db.dataset_id}:{index}".encode(),
            {
                "policy_id": f"{db.dataset_id}:{index}",
                "version": "1",
                "decision_state": "approved_scoped",
                "permitted_uses": uses,
                "scope": "Synthetic HTTP rights boundary",
            },
            {"synthetic": True},
        )
        objects.attach_batch_object(db.publisher, db.dataset_id, batch, staged)
        snapshot = f"S{index}"
        objects.publish_snapshot(db.publisher, db.dataset_id, snapshot, [batch], parent, uuid4())
        parent = snapshot
        stages.append(staged)
    return stages


def test_comparison_export_and_matrix_recheck_both_snapshots_current_rights(
    db, delivery, tmp_path, monkeypatch
):
    monkeypatch.setenv("CHRONICLE_OBJECT_ROOT", str(tmp_path / "objects"))
    stages = publish_rights_pair(db)
    old, new = report_via_http(delivery, "S1"), report_via_http(delivery, "S2")
    comparison_body = {"old_report_id": old["report_id"], "new_report_id": new["report_id"]}
    allowed = delivery.client.post(
        delivery.root + "/comparisons", json=comparison_body, headers=delivery.headers
    )
    assert allowed.status_code == 200 and allowed.json()["rows"][0]["value"] == "3.000000000000"
    denied = delivery.client.post(
        delivery.root + "/comparison-export", json=comparison_body, headers=delivery.headers
    )
    assert denied.status_code == 403 and denied.json()["code"] == "RIGHTS_DENIED"
    objects.revoke_rights(db.publisher, stages[1].policy_sha256, "Live HTTP boundary test")
    for left, right in ((old, new), (new, old)):
        for endpoint in ("comparisons", "comparison-export"):
            response = delivery.client.post(
                delivery.root + "/" + endpoint,
                json={
                    "old_report_id": left["report_id"],
                    "new_report_id": right["report_id"],
                },
                headers=delivery.headers,
            )
            assert response.status_code == 403, response.text
            assert response.json()["code"] == "RIGHTS_DENIED"
        response = delivery.client.post(
            delivery.root + "/matrix",
            json={
                "old_context_id": left["context_id"],
                "new_context_id": right["context_id"],
                "comparison_id": str(uuid4()),
                "formula": "employment_delta",
            },
            headers=delivery.headers,
        )
        assert response.status_code == 403 and response.json()["code"] == "RIGHTS_DENIED"
    assert (
        delivery.client.get(
            delivery.root + "/reports/" + old["report_id"], headers=delivery.headers
        ).status_code
        == 200
    )
    assert (
        delivery.client.get(
            delivery.root + "/reports/" + new["report_id"], headers=delivery.headers
        ).status_code
        == 403
    )


def test_changed_unit_metadata_does_not_become_numeric_comparison_or_attribution(db, delivery):
    fixture = copy.deepcopy(G1)
    fixture["revisions"][2]["unit"]["metadata_version"] = "v2"
    december = copy.deepcopy(fixture["revisions"][2])
    december.update(observation_month="2025-12-01", value="90")
    fixture["revisions"].append(december)
    import_fixture(db, fixture)
    old = report_via_http(delivery, "S1", expected_unit=False)
    new = report_via_http(delivery, "S2", expected_unit=False)
    assert old["rows"][0]["value"] == "10.000000000000"
    assert new["rows"][0]["value"] == "13.000000000000"
    response = delivery.client.post(
        delivery.root + "/comparisons",
        json={
            "old_report_id": old["report_id"],
            "new_report_id": new["report_id"],
        },
        headers=delivery.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["rows"][0]["status"] == "unit_mismatch"
    assert response.json()["rows"][0]["value"] is None
    response = delivery.client.post(
        delivery.root + "/matrix",
        json={
            "old_context_id": old["context_id"],
            "new_context_id": new["context_id"],
            "comparison_id": str(uuid4()),
            "formula": "employment_delta",
        },
        headers=delivery.headers,
    )
    row = response.json()["rows"][0]
    assert row["status"] == "unit_mismatch"
    assert all(
        row[key] is None for key in ("data_effect", "code_effect", "interaction", "total_change")
    )


def test_live_identity_revocation_blocks_already_retained_report_and_operator_endpoint(
    db, delivery
):
    import_fixture(db, G1)
    report = report_via_http(delivery)
    assert (
        delivery.client.get(delivery.root + "/operations", headers=delivery.headers).status_code
        == 403
    )
    policy = json.loads(delivery.policy.read_text())
    policy["identities"][0]["enabled"] = False
    delivery.policy.write_text(json.dumps(policy))
    response = delivery.client.get(
        delivery.root + "/reports/" + report["report_id"], headers=delivery.headers
    )
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
