"""Dataset-scoped bounded series discovery and incremental dataset boundaries."""

import json
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient

from chronicle import incremental, reports, store
from chronicle.access import configure_access
from chronicle.api import create_app
from chronicle.domain import Query
from chronicle.errors import ContractError
from tests.postgres.conftest import import_fixture


def test_series_discovery_is_scoped_bounded_deterministic_and_authenticated(db, tmp_path):
    policy, credentials = configure_access(tmp_path / "access", [db.dataset_id])
    token = json.loads(credentials.read_text())["viewer"]
    headers = {"Authorization": "Bearer " + token}
    with db.importer.transaction():
        db.importer.execute(
            "INSERT INTO chronicle.source_namespace VALUES (%s,%s,%s)",
            (db.dataset_id, "synthetic", "documented_sequence"),
        )
        for index in reversed(range(101)):
            db.importer.execute(
                "INSERT INTO chronicle.series VALUES (%s,%s,%s)",
                (db.dataset_id, "synthetic", f"series-{index:03}"),
            )
    foreign = "foreign-" + uuid4().hex
    store.create_dataset(db.importer, foreign)
    db.importer.execute(
        "INSERT INTO chronicle.source_namespace VALUES (%s,%s,%s)",
        (foreign, "foreign-only", "documented_sequence"),
    )
    db.importer.execute(
        "INSERT INTO chronicle.series VALUES (%s,%s,%s)", (foreign, "foreign-only", "private")
    )
    with TestClient(create_app(access_path=policy, connect=db.connect)) as client:
        route = f"/v1/datasets/{db.dataset_id}/series"
        assert client.get(route).status_code == 401
        result = client.get(route, headers=headers)
        assert result.status_code == 200
        assert result.json() == {
            "limit": 100,
            "truncated": True,
            "series": [
                {"source_namespace": "synthetic", "series_key": f"series-{index:03}"}
                for index in range(100)
            ],
        }
        forbidden = client.get(f"/v1/datasets/{foreign}/series", headers=headers)
        assert forbidden.status_code == 403
        assert "private" not in forbidden.text


def test_incremental_rejects_identical_inputs_from_another_dataset_without_partial_report(db):
    from pathlib import Path

    fixture = json.loads((Path(__file__).parents[1] / "fixtures/golden/G1.json").read_text())
    import_fixture(db, fixture)
    foreign = SimpleNamespace(**vars(db))
    foreign.dataset_id = "same-inputs-" + uuid4().hex
    store.create_dataset(db.importer, foreign.dataset_id)
    import_fixture(foreign, fixture)
    query = Query("system_snapshot", "S1", "synthetic", "employment", (date(2026, 1, 1),))
    context = store.create_context(db.reader, db.dataset_id, query)
    other_context = store.create_context(db.reader, foreign.dataset_id, query)
    old = reports.build_report(db.reader, context, "employment_delta")
    report_id = uuid4()
    with pytest.raises(ContractError) as caught:
        incremental.build_incremental_report(
            db.reader, UUID(old["report_id"]), other_context, report_id=report_id
        )
    assert caught.value.code == "INCOMPATIBLE_CONTEXT"
    assert db.reader.execute(
        "SELECT count(*) FROM chronicle.report_run WHERE report_id=%s", (report_id,)
    ).fetchone() == (0,)
    assert db.reader.execute(
        "SELECT count(*) FROM chronicle.analysis_context WHERE context_id=%s",
        (uuid5(report_id, "chronicle.report.inputs.v1"),),
    ).fetchone() == (0,)
