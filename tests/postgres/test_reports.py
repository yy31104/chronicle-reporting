"""Actual PostgreSQL formula, pinned halo, retained identity and role boundaries."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import psycopg
import pytest

from chronicle import reports, store
from chronicle.domain import Query, Unit
from chronicle.errors import ContractError
from tests.postgres.conftest import import_fixture
from tests.support import query_from_fixture, revision_from_fixture

G1 = json.loads((Path(__file__).parents[1] / "fixtures/golden/G1.json").read_text())


def production_context(db, values, *, statuses=None, units=None):
    months = ["2025-11-01", "2025-12-01", "2026-01-01"]
    rows = []
    for index, (month, value) in enumerate(zip(months, values, strict=True)):
        raw = copy.deepcopy(G1["revisions"][0])
        raw.update(
            series_key="production",
            observation_month=month,
            value=value,
            status=statuses[index] if statuses else "value",
            unit=units[index] if units else {"name": "index", "metadata_version": "v1"},
        )
        rows.append(revision_from_fixture(raw))
    store.import_batch(db.importer, db.dataset_id, "B1", tuple(rows))
    store.publish_snapshot(db.publisher, db.dataset_id, "S1", ["B1"], "0" * 64, None)
    return store.create_context(
        db.reader,
        db.dataset_id,
        Query("system_snapshot", "S1", "synthetic", "production", (date(2026, 1, 1),)),
    )


@pytest.mark.parametrize("case", G1["queries"][:4], ids=lambda case: case["name"])
def test_g1_report_values_preserve_real_context_and_full_evidence(db, case):
    import_fixture(db, G1)
    query = copy.deepcopy(case)
    query["months"] = ["2026-01-01"]
    context = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    report = reports.build_report(db.reader, context, "employment_delta")
    assert report["rows"][0]["value"] == case["expected_january_delta"]
    assert [item["observation_month"] for item in report["rows"][0]["inputs"]] == [
        "2025-12-01",
        "2026-01-01",
    ]
    assert all(item["snapshot_id"] == case["snapshot_id"] for item in report["rows"][0]["inputs"])
    assert all(item["selected_revision"]["occurrences"] for item in report["rows"][0]["inputs"])
    assert db.reader.execute(
        "SELECT months FROM chronicle.analysis_context WHERE context_id=%s", (context,)
    ).fetchone()[0] == [date(2026, 1, 1)]
    assert reports.replay_report(db.reader, UUID(report["report_id"])) == report
    assert reports.reexecute_report(db.reader, UUID(report["report_id"]))["status"] == "verified"


def test_production_mean_loads_two_calendar_lookbacks(db):
    context = production_context(db, ["0", "0", "1"])
    report = reports.build_report(db.reader, context, "production_mean3")
    assert report["rows"][0]["value"] == "0.333333333333"
    assert [item["observation_month"] for item in report["rows"][0]["inputs"]] == [
        "2025-11-01",
        "2025-12-01",
        "2026-01-01",
    ]


def test_missing_calendar_month_is_preserved_as_unavailable_input(db):
    raw = copy.deepcopy(G1)
    raw["revisions"] = [raw["revisions"][1]]
    raw["revisions"][0].update(series_key="production", value="100")
    raw["snapshots"] = {"S1": ["B1"]}
    import_fixture(db, raw)
    context = store.create_context(
        db.reader,
        db.dataset_id,
        Query("system_snapshot", "S1", "synthetic", "production", (date(2026, 1, 1),)),
    )
    row = reports.build_report(db.reader, context, "production_mean3")["rows"][0]
    assert row["value"] is None
    assert row["status"] == "not_available_at_cutoff"
    assert len(row["reasons"]) == 2
    assert all(reason["message_zh"] == "No data in this snapshot" for reason in row["reasons"])


@pytest.mark.parametrize("status", ["missing", "withdrawn"])
def test_production_mean_cannot_skip_source_failures(db, status):
    context = production_context(db, ["0", None, "1"], statuses=["value", status, "value"])
    row = reports.build_report(db.reader, context, "production_mean3")["rows"][0]
    assert row["status"] == status and row["value"] is None
    assert row["reasons"][0]["observation_month"] == "2025-12-01"


def test_metadata_change_within_mean_window_is_not_averaged(db):
    context = production_context(
        db,
        ["100", "100", "100"],
        units=[
            {"name": "index", "metadata_version": "base2017"},
            {"name": "index", "metadata_version": "base2022"},
            {"name": "index", "metadata_version": "base2022"},
        ],
    )
    row = reports.build_report(db.reader, context, "production_mean3")["rows"][0]
    assert row["status"] == "unit_mismatch" and row["value"] is None


@pytest.mark.parametrize("status", ["missing", "withdrawn"])
def test_source_status_overrides_unit_mismatch_only_in_display(db, status):
    production_context(
        db,
        ["100", None, "100"],
        statuses=["value", status, "value"],
        units=[
            {"name": "index", "metadata_version": "base2017"},
            {"name": "index", "metadata_version": "base2022"},
            {"name": "index", "metadata_version": "base2017"},
        ],
    )
    context = store.create_context(
        db.reader,
        db.dataset_id,
        Query(
            "system_snapshot",
            "S1",
            "synthetic",
            "production",
            (date(2026, 1, 1),),
            expected_unit=Unit("index", "base2017"),
        ),
    )
    row = reports.build_report(db.reader, context, "production_mean3")["rows"][0]
    assert row["status"] == status and row["value"] is None
    assert row["inputs"][1]["status"] == "unit_mismatch"
    assert row["inputs"][1]["display"]["display_status"] == status
    assert row["inputs"][1]["source_status"] == status
    assert row["warnings"] == ["unit_mismatch"]


def test_unknown_asof_blocks_a_report_and_retains_blocking_evidence(db):
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/golden/G4.json").read_text())
    import_fixture(db, fixture)
    query = next(q for q in fixture["queries"] if q["name"] == "higher_unknown_blocks_known_value")
    context = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    row = reports.build_report(db.reader, context, "employment_delta")["rows"][0]
    assert row["status"] == "unknown_availability" and row["value"] is None
    january = row["inputs"][1]
    assert january["selected_revision"] is None
    assert january["blocking_revision"]["availability"]["precision"] == "unknown"


def test_identity_replay_and_reexecution_survive_pointer_movement(db):
    first = copy.deepcopy(G1)
    first["snapshots"] = {"S1": ["B1"]}
    import_fixture(db, first)
    context = store.resolve_current_context(
        db.reader, db.dataset_id, "latest_corrected", "synthetic", "employment", (date(2026, 1, 1),)
    )
    report_id = uuid4()
    original = reports.build_report(db.reader, context, "employment_delta", report_id=report_id)
    store.publish_snapshot(db.publisher, db.dataset_id, "S2", ["B1", "B2"], "f" * 64, "S1")
    assert reports.replay_report(db.reader, report_id) == original
    assert reports.reexecute_report(db.reader, report_id)["document"] == original
    assert (
        reports.build_report(db.reader, context, "employment_delta", report_id=report_id)
        == original
    )
    assert original["rows"][0]["value"] == "10.000000000000"
    newer = store.resolve_current_context(
        db.reader, db.dataset_id, "latest_corrected", "synthetic", "employment", (date(2026, 1, 1),)
    )
    assert reports.build_report(db.reader, newer, "employment_delta")["rows"][0]["value"] == (
        "13.000000000000"
    )


def test_same_report_identity_different_parameters_rejects(db):
    context = production_context(db, ["0", "0", "1"])
    report_id = uuid4()
    original = reports.build_report(db.reader, context, "production_mean3", report_id=report_id)
    with pytest.raises(ContractError) as failure:
        reports.build_report(db.reader, context, "employment_delta", report_id=report_id)
    assert failure.value.code == "REPORT_IDENTITY_CONFLICT"
    assert reports.replay_report(db.reader, report_id) == original


def test_concurrent_same_report_identity_returns_one_identical_document(db):
    context = production_context(db, ["0", "0", "1"])
    report_id = uuid4()
    first, second = db.connect("reader"), db.connect("reader")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                reports.build_report, conn, context, "production_mean3", report_id=report_id
            )
            for conn in (first, second)
        ]
        documents = [future.result(timeout=10) for future in futures]
    assert documents[0] == documents[1]
    assert db.reader.execute(
        "SELECT count(*) FROM chronicle.report_run WHERE report_id=%s", (report_id,)
    ).fetchone() == (1,)


def test_overflow_rolls_back_the_whole_report_and_new_halo_context(db):
    raw = copy.deepcopy(G1)
    raw["revisions"] = raw["revisions"][:2]
    raw["revisions"][0]["value"] = "-99999999999999999999999999"
    raw["revisions"][1]["value"] = "99999999999999999999999999"
    raw["snapshots"] = {"S1": ["B1"]}
    import_fixture(db, raw)
    context = store.create_context(
        db.reader,
        db.dataset_id,
        Query("system_snapshot", "S1", "synthetic", "employment", (date(2026, 1, 1),)),
    )
    report_id = uuid4()
    with pytest.raises(ContractError) as failure:
        reports.build_report(db.reader, context, "employment_delta", report_id=report_id)
    assert failure.value.code == "NUMERIC_OUT_OF_RANGE"
    assert db.reader.execute(
        "SELECT count(*) FROM chronicle.report_run WHERE report_id=%s", (report_id,)
    ).fetchone() == (0,)
    halo = uuid5(report_id, "chronicle.report.inputs.v1")
    assert db.reader.execute(
        "SELECT count(*) FROM chronicle.analysis_context WHERE context_id=%s", (halo,)
    ).fetchone() == (0,)


def test_missing_execution_environment_never_silently_upgrades_replay(db, monkeypatch):
    context = production_context(db, ["0", "0", "1"])
    report = reports.build_report(db.reader, context, "production_mean3")
    report_id = UUID(report["report_id"])
    identity = {**report["implementation"], "python": "unavailable-prior-runtime"}
    monkeypatch.setattr(reports, "implementation_identity", lambda: identity)
    assert reports.replay_report(db.reader, report_id) == report
    with pytest.raises(ContractError) as failure:
        reports.reexecute_report(db.reader, report_id)
    assert failure.value.code == "ENVIRONMENT_UNAVAILABLE"


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_retained_reports_are_immutable_even_for_owner(db, operation):
    context = production_context(db, ["0", "0", "1"])
    report = reports.build_report(db.reader, context, "production_mean3")
    commands = {
        "UPDATE": "UPDATE chronicle.report_run SET document=document",
        "DELETE": "DELETE FROM chronicle.report_run",
        "TRUNCATE": "TRUNCATE chronicle.report_run",
    }
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        with db.admin.transaction():
            db.admin.execute("SET LOCAL ROLE chronicle_owner")
            db.admin.execute(commands[operation])
    assert reports.replay_report(db.reader, UUID(report["report_id"])) == report


def test_unrecognized_report_is_explicit_and_output_grid_cannot_expand_base(db):
    with pytest.raises(ContractError) as failure:
        reports.replay_report(db.reader, uuid4())
    assert failure.value.code == "REPORT_NOT_FOUND"
    context = production_context(db, ["0", "0", "1"])
    with pytest.raises(ContractError) as failure:
        reports.build_report(
            db.reader, context, "production_mean3", output_months=(date(2026, 2, 1),)
        )
    assert failure.value.code == "INVALID_GRID"
