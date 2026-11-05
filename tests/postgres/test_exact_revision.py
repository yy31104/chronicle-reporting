"""Real PostgreSQL checks for the exact, once-rounded revision difference.

Inputs are explicitly synthetic. The production series below reuses the shape of the
real G.17 January 2024 revision (102.7, 102.7, 102.6 -> 103.0, 102.7, 102.2) so that
the two roundings visibly disagree, but it is imported into the synthetic namespace
of a temporary test dataset and claims nothing about any publisher. Every expected
decimal was worked out by hand before the API/CLI wiring existed:

    old mean 308.0 / 3 -> 102.666666666667;  new mean 307.9 / 3 -> 102.633333333333
    stored-output difference -0.033333333334;  exact -1/30 -> -0.033333333333
"""

import hashlib
import json
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from chronicle import cli, comparison, objects, store
from chronicle.domain import Occurrence, Query, Revision, SourceAvailability, Unit
from chronicle.errors import ContractError
from chronicle.exact_revision import compare_reports_exact
from chronicle.reports import build_report, replay_report
from tests.postgres import test_delivery_edges as edges
from tests.postgres.conftest import database as database_fixture
from tests.postgres.conftest import import_fixture

INDEX = {"name": "index", "metadata_version": "v1"}
JANUARY = date(2026, 1, 1)
G1 = edges.G1
publish_rights_pair = edges.publish_rights_pair
delivery = edges.delivery  # reuse the scoped HTTP client fixture


@pytest.fixture
def database():
    # Rights cases add global object entries; keep each catalog paired with its root.
    yield from database_fixture.__wrapped__()


def revision(month, value, *, revised=False, status="value"):
    return Revision(
        "synthetic",
        "production",
        date.fromisoformat(month),
        "revised" if revised else "initial",
        20 if revised else 10,
        "documented_sequence",
        SourceAvailability("date", date(2026, 3, 15) if revised else date(2026, 2, 15)),
        value,
        status,
        Unit("index", "v1"),
        (
            Occurrence(
                "B2" if revised else "B1",
                datetime(2026, 3, 15, tzinfo=UTC) if revised else datetime(2026, 2, 15, tzinfo=UTC),
            ),
        ),
    )


def seed_production(db, *, january=("102.2", "value")):
    store.import_batch(
        db.importer,
        db.dataset_id,
        "B1",
        (
            revision("2025-11-01", "102.7"),
            revision("2025-12-01", "102.7"),
            revision("2026-01-01", "102.6"),
        ),
    )
    value, status = january
    store.import_batch(
        db.importer,
        db.dataset_id,
        "B2",
        (
            revision("2025-11-01", "103.0", revised=True),
            revision("2026-01-01", value, revised=True, status=status),
        ),
    )
    manifest = hashlib.sha256(b"synthetic exact-difference test").hexdigest()
    store.publish_snapshot(db.publisher, db.dataset_id, "S1", ["B1"], manifest, None)
    store.publish_snapshot(db.publisher, db.dataset_id, "S2", ["B1", "B2"], manifest, "S1")


def production_reports(db):
    result = []
    for snapshot in ("S1", "S2"):
        context = store.create_context(
            db.reader,
            db.dataset_id,
            Query(
                "system_snapshot",
                snapshot,
                "synthetic",
                "production",
                (JANUARY,),
                None,
                Unit("index", "v1"),
            ),
        )
        result.append(build_report(db.reader, context, "production_mean3"))
    return result


def report_count(db):
    return db.admin.execute("SELECT count(*) FROM chronicle.report_run").fetchone()[0]


def test_db_wrapper_keeps_stored_difference_and_rounds_exact_difference_once(db):
    seed_production(db)
    old, new = production_reports(db)
    assert (old["rows"][0]["value"], new["rows"][0]["value"]) == (
        "102.666666666667",
        "102.633333333333",
    )
    before = report_count(db)
    result = compare_reports_exact(db.reader, old["report_id"], new["report_id"])
    assert result["schema"] == "chronicle.report-comparison-exact.v1"
    # The existing comparison is embedded byte-for-byte, from the unchanged function.
    assert result["stored_comparison"] == comparison.compare_reports(
        db.reader, old["report_id"], new["report_id"]
    )
    assert result["rows"] == [
        {
            "observation_month": "2026-01-01",
            "status": "value",
            "unit": INDEX,
            "old_status": "value",
            "new_status": "value",
            "stored_output_difference": "-0.033333333334",
            "exact_difference": "-0.033333333333",
            "exact_difference_rational": {"numerator": "-1", "denominator": "30"},
            "rounding_gap": "-0.000000000001",
        }
    ]
    # Read-only: no report is created and retained documents are unchanged.
    assert report_count(db) == before
    assert replay_report(db.reader, old["report_id"]) == old
    assert replay_report(db.reader, new["report_id"]) == new


def test_db_wrapper_employment_has_no_gap(db):
    import_fixture(db, G1)
    reports = []
    for snapshot in ("S1", "S2"):
        context = store.create_context(
            db.reader,
            db.dataset_id,
            Query("system_snapshot", snapshot, "synthetic", "employment", (JANUARY,)),
        )
        reports.append(build_report(db.reader, context, "employment_delta"))
    (row,) = compare_reports_exact(db.reader, reports[0]["report_id"], reports[1]["report_id"])[
        "rows"
    ]
    assert row["stored_output_difference"] == row["exact_difference"] == "3.000000000000"
    assert row["exact_difference_rational"] == {"numerator": "3", "denominator": "1"}
    assert row["rounding_gap"] == "0.000000000000"


def test_db_wrapper_withdrawn_value_is_incomplete_not_zero(db):
    seed_production(db, january=(None, "withdrawn"))
    old, new = production_reports(db)
    assert new["rows"][0]["status"] == "withdrawn" and new["rows"][0]["value"] is None
    (row,) = compare_reports_exact(db.reader, old["report_id"], new["report_id"])["rows"]
    assert row["status"] == "incomplete"
    assert row["new_status"] == "withdrawn"
    assert row["stored_output_difference"] is None
    assert row["exact_difference"] is None
    assert row["exact_difference_rational"] is None
    assert row["rounding_gap"] is None


def test_db_wrapper_unknown_report_is_not_found(db):
    seed_production(db)
    old, _ = production_reports(db)
    with pytest.raises(ContractError) as failure:
        compare_reports_exact(db.reader, old["report_id"], uuid4())
    assert failure.value.code == "REPORT_NOT_FOUND"


def test_db_wrapper_detects_corrupt_retained_report(db):
    seed_production(db)
    old, new = production_reports(db)
    with db.admin.transaction():
        db.admin.execute("ALTER TABLE chronicle.report_run DISABLE TRIGGER immutable_rows")
        db.admin.execute(
            "UPDATE chronicle.report_run SET document=jsonb_set(document,"
            "'{rows,0,value}','\"102.666666666666\"') WHERE report_id=%s",
            (old["report_id"],),
        )
        db.admin.execute("ALTER TABLE chronicle.report_run ENABLE TRIGGER immutable_rows")
    with pytest.raises(ContractError) as failure:
        compare_reports_exact(db.reader, old["report_id"], new["report_id"])
    assert failure.value.code == "REPORT_CORRUPT"


def http_production_report(delivery, snapshot):
    context_id = str(uuid4())
    response = delivery.client.post(
        delivery.root + "/contexts",
        json={
            "context_id": context_id,
            "snapshot_id": snapshot,
            "mode": "system_snapshot",
            "source_namespace": "synthetic",
            "series_key": "production",
            "months": ["2026-01-01"],
            "expected_unit": INDEX,
        },
        headers=delivery.headers,
    )
    assert response.status_code == 200, response.text
    response = delivery.client.post(
        delivery.root + "/reports",
        json={"context_id": context_id, "report_id": str(uuid4()), "formula": "production_mean3"},
        headers=delivery.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_http_exact_endpoint_returns_both_differences_beside_unchanged_endpoint(db, delivery):
    seed_production(db)
    old, new = http_production_report(delivery, "S1"), http_production_report(delivery, "S2")
    body = {"old_report_id": old["report_id"], "new_report_id": new["report_id"]}
    exact = delivery.client.post(
        delivery.root + "/exact-comparisons", json=body, headers=delivery.headers
    )
    assert exact.status_code == 200, exact.text
    stored = delivery.client.post(
        delivery.root + "/comparisons", json=body, headers=delivery.headers
    )
    assert stored.status_code == 200
    # The existing endpoint is unchanged and is exactly the embedded stored comparison.
    assert stored.json()["rows"][0]["value"] == "-0.033333333334"
    assert exact.json()["stored_comparison"] == stored.json()
    row = exact.json()["rows"][0]
    assert row["stored_output_difference"] == "-0.033333333334"
    assert row["exact_difference"] == "-0.033333333333"
    assert row["rounding_gap"] == "-0.000000000001"
    unauthenticated = delivery.client.post(delivery.root + "/exact-comparisons", json=body)
    assert unauthenticated.status_code == 401


def test_http_exact_endpoint_rejects_foreign_report_in_either_position(db, delivery):
    seed_production(db)
    own = http_production_report(delivery, "S1")
    foreign = type(db)(**vars(db))
    foreign.dataset_id = "foreign-" + uuid4().hex
    store.create_dataset(db.importer, foreign.dataset_id)
    seed_production(foreign)
    other = production_reports(foreign)[1]
    policy = json.loads(delivery.policy.read_text())
    policy["identities"][0]["datasets"].append(foreign.dataset_id)
    delivery.policy.write_text(json.dumps(policy))
    for left, right in ((own, other), (other, own)):
        response = delivery.client.post(
            delivery.root + "/exact-comparisons",
            json={"old_report_id": left["report_id"], "new_report_id": right["report_id"]},
            headers=delivery.headers,
        )
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "REPORT_NOT_FOUND"
        assert "rows" not in response.json()


def test_http_exact_endpoint_rechecks_current_rights_of_both_snapshots(
    db, delivery, tmp_path, monkeypatch
):
    monkeypatch.setenv("CHRONICLE_OBJECT_ROOT", str(tmp_path / "objects"))
    stages = publish_rights_pair(db)
    reports = []
    for snapshot in ("S1", "S2"):
        context = store.create_context(
            db.reader,
            db.dataset_id,
            Query(
                "system_snapshot",
                snapshot,
                "synthetic",
                "employment",
                (JANUARY,),
                None,
                Unit("thousand_persons", "v1"),
            ),
        )
        reports.append(build_report(db.reader, context, "employment_delta"))
    body = {"old_report_id": reports[0]["report_id"], "new_report_id": reports[1]["report_id"]}
    allowed = delivery.client.post(
        delivery.root + "/exact-comparisons", json=body, headers=delivery.headers
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["rows"][0]["exact_difference"] == "3.000000000000"
    objects.revoke_rights(db.publisher, stages[1].policy_sha256, "Exact comparison rights test")
    for left, right in ((0, 1), (1, 0)):
        response = delivery.client.post(
            delivery.root + "/exact-comparisons",
            json={
                "old_report_id": reports[left]["report_id"],
                "new_report_id": reports[right]["report_id"],
            },
            headers=delivery.headers,
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "RIGHTS_DENIED"


def test_cli_compare_exact_flag_and_unchanged_default(db, monkeypatch, capsys):
    seed_production(db)
    old, new = production_reports(db)
    monkeypatch.setenv("CHRONICLE_DB_NAME", db.database)
    assert cli.main(["compare", old["report_id"], new["report_id"]]) == 0
    default = json.loads(capsys.readouterr().out)
    assert default == comparison.compare_reports(db.reader, old["report_id"], new["report_id"])
    assert cli.main(["compare", old["report_id"], new["report_id"], "--exact"]) == 0
    exact = json.loads(capsys.readouterr().out)
    assert exact["stored_comparison"] == default
    assert exact["rows"][0]["stored_output_difference"] == "-0.033333333334"
    assert exact["rows"][0]["exact_difference"] == "-0.033333333333"
    assert exact["rows"][0]["exact_difference_rational"] == {
        "numerator": "-1",
        "denominator": "30",
    }
