"""Retained four-run comparisons, incremental arithmetic and reproducibility."""

import copy
import io
import json
import random
import zipfile
from datetime import date
from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest

from chronicle import bundles, comparison, incremental, objects, reports, store
from chronicle.domain import Query
from chronicle.errors import ContractError
from tests.postgres.conftest import database as database_fixture
from tests.postgres.conftest import import_fixture
from tests.support import revision_from_fixture

G1 = json.loads((Path(__file__).parents[1] / "fixtures/golden/G1.json").read_text())


@pytest.fixture
def database():
    # Object catalog entries are global by digest. Pair each temporary root with
    # its own database so later readiness checks never inspect vanished test roots.
    yield from database_fixture.__wrapped__()


def paired_contexts(db):
    import_fixture(db, G1)
    return tuple(
        store.create_context(
            db.reader,
            db.dataset_id,
            Query("system_snapshot", snapshot, "synthetic", "employment", (date(2026, 1, 1),)),
        )
        for snapshot in ("S1", "S2")
    )


def test_real_four_run_matrix_and_frozen_identity(db):
    old, new = paired_contexts(db)
    comparison_id = uuid4()
    result = comparison.compare_matrix(
        db.reader, old, new, "employment_delta", comparison_id=comparison_id
    )
    assert [
        result["children"][key]["rows"][0]["value"]
        for key in ("old_old", "new_old", "old_new", "new_new")
    ] == ["10.000000000000", "13.000000000000", "20.000000000000", "26.000000000000"]
    assert len({run["run_id"] for run in result["children"].values()}) == 4
    assert result["rows"][0] == {
        "observation_month": "2026-01-01",
        "status": "value",
        "unit": {"name": "thousand_persons", "metadata_version": "v1"},
        "data_effect": "3.000000000000",
        "code_effect": "10.000000000000",
        "interaction": "3.000000000000",
        "total_change": "16.000000000000",
    }
    assert comparison.replay_comparison(db.reader, comparison_id) == result
    assert (
        comparison.compare_matrix(
            db.reader, old, new, "employment_delta", comparison_id=comparison_id
        )
        == result
    )
    with pytest.raises(ContractError) as failure:
        comparison.compare_matrix(
            db.reader,
            old,
            new,
            "employment_delta",
            comparison_id=comparison_id,
            new_code="fixed-v1",
        )
    assert failure.value.code == "COMPARISON_IDENTITY_CONFLICT"
    with pytest.raises(ContractError) as failure:
        comparison.compare_matrix(
            db.reader, old, new, "employment_delta", comparison_id=comparison_id, output_months=()
        )
    assert failure.value.code == "INVALID_GRID"


def test_fixed_version_is_executable_on_both_data_contexts_without_synthetic_semantic_change(db):
    old, new = paired_contexts(db)
    result = comparison.compare_matrix(db.reader, old, new, "employment_delta", new_code="fixed-v1")
    assert result["rows"][0]["code_effect"] == result["rows"][0]["interaction"] == "0.000000000000"


def test_incompatible_matrix_controls_roll_back_child_reports(db):
    old, new = paired_contexts(db)
    other = store.create_context(
        db.reader,
        db.dataset_id,
        Query("latest_corrected", "S2", "synthetic", "employment", (date(2026, 1, 1),)),
    )
    before = db.reader.execute("SELECT count(*) FROM chronicle.report_run").fetchone()
    with pytest.raises(ContractError) as failure:
        comparison.compare_matrix(db.reader, old, other, "employment_delta")
    assert failure.value.code == "INCOMPATIBLE_CONTEXT"
    assert db.reader.execute("SELECT count(*) FROM chronicle.report_run").fetchone() == before
    with pytest.raises(ContractError) as failure:
        comparison.compare_matrix(
            db.reader, old, new, "employment_delta", old_code="lost-executable"
        )
    assert failure.value.code == "ENVIRONMENT_UNAVAILABLE"


def test_plain_comparison_uses_stored_canonical_values(db):
    old, new = paired_contexts(db)
    old_report = reports.build_report(db.reader, old, "employment_delta")
    new_report = reports.build_report(db.reader, new, "employment_delta")
    result = comparison.compare_reports(
        db.reader, UUID(old_report["report_id"]), UUID(new_report["report_id"])
    )
    assert result["rows"][0]["value"] == "3.000000000000"


def production_pair(db, *, initial_absence=False, revised_status="value", revised_unit=None):
    months = ["2025-11-01", "2025-12-01", "2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"]
    initial = []
    for month, value in zip(months, ["1", "2", "3", "4", "5", "6"], strict=True):
        if initial_absence and month == "2025-12-01":
            continue
        raw = copy.deepcopy(G1["revisions"][0])
        raw.update(
            series_key="production",
            observation_month=month,
            value=value,
            unit={"name": "index", "metadata_version": "v1"},
        )
        initial.append(revision_from_fixture(raw))
    store.import_batch(db.importer, db.dataset_id, "B1", tuple(initial))
    raw = copy.deepcopy(G1["revisions"][2])
    raw.update(
        series_key="production",
        observation_month="2025-12-01",
        value="20" if revised_status == "value" else None,
        status=revised_status,
        unit=revised_unit or {"name": "index", "metadata_version": "v1"},
    )
    store.import_batch(db.importer, db.dataset_id, "B2", (revision_from_fixture(raw),))
    store.publish_snapshot(db.publisher, db.dataset_id, "S1", ["B1"], "0" * 64, None)
    store.publish_snapshot(db.publisher, db.dataset_id, "S2", ["B1", "B2"], "1" * 64, "S1")
    return tuple(
        store.create_context(
            db.reader,
            db.dataset_id,
            Query(
                "system_snapshot",
                snapshot,
                "synthetic",
                "production",
                tuple(date(2026, m, 1) for m in range(1, 5)),
            ),
        )
        for snapshot in ("S1", "S2")
    )


@pytest.mark.parametrize(
    "initial_absence,revised_status", [(False, "value"), (True, "value"), (False, "withdrawn")]
)
def test_incremental_equals_full_report_with_backfill_withdrawal_and_lookback(
    db, initial_absence, revised_status
):
    old, new = production_pair(db, initial_absence=initial_absence, revised_status=revised_status)
    prior = reports.build_report(db.reader, old, "production_mean3")
    actual = incremental.build_incremental_report(db.reader, UUID(prior["report_id"]), new)
    complete = reports.build_report(db.reader, new, "production_mean3")
    assert actual["rows"] == complete["rows"]
    assert actual["execution_result"]["reused_months"] == ["2026-03-01", "2026-04-01"]
    assert actual["execution_result"]["recomputed_months"] == ["2026-01-01", "2026-02-01"]
    assert all(item["snapshot_id"] == "S2" for row in actual["rows"] for item in row["inputs"])
    if revised_status == "value":
        # Independent full arithmetic, with direct literal inputs and no production helpers.
        expected = [Fraction(24, 3), Fraction(27, 3), Fraction(12, 3), Fraction(15, 3)]
        assert [Fraction(row["value"]) for row in actual["rows"]] == expected
    else:
        assert [row["status"] for row in actual["rows"]] == [
            "withdrawn",
            "withdrawn",
            "value",
            "value",
        ]


def test_incremental_actually_skips_unchanged_arithmetic(db, monkeypatch):
    old, new = production_pair(db)
    prior = reports.build_report(db.reader, old, "production_mean3")
    original = reports.canonical_ratio
    calls = []

    def counted(numerator, denominator):
        calls.append((numerator, denominator))
        return original(numerator, denominator)

    monkeypatch.setattr(reports, "canonical_ratio", counted)
    result = incremental.build_incremental_report(db.reader, UUID(prior["report_id"]), new)
    assert len(calls) == 2
    assert result["execution_result"]["strategy"] == "incremental"


def test_changed_metadata_forces_documented_full_rebuild(db):
    old, new = production_pair(db, revised_unit={"name": "index", "metadata_version": "v2"})
    prior = reports.build_report(db.reader, old, "production_mean3")
    result = incremental.build_incremental_report(db.reader, UUID(prior["report_id"]), new)
    assert result["execution_result"]["strategy"] == "full"
    assert result["execution_result"]["fallback_reason"] == "unit_metadata_changed"
    assert result["execution_result"]["reused_months"] == []
    assert result["rows"] == reports.build_report(db.reader, new, "production_mean3")["rows"]


def test_changed_implementation_forces_full_rebuild(db, monkeypatch):
    old, new = production_pair(db)
    prior = reports.build_report(db.reader, old, "production_mean3")
    changed = {**reports.implementation_identity(), "python": "new-runtime"}
    monkeypatch.setattr(reports, "implementation_identity", lambda: changed)
    result = incremental.build_incremental_report(db.reader, UUID(prior["report_id"]), new)
    assert result["execution_result"]["strategy"] == "full"
    assert result["execution_result"]["fallback_reason"] == "implementation_changed"
    assert result["rows"] == reports.build_report(db.reader, new, "production_mean3")["rows"]


def test_missing_forward_halo_mutant_produces_detectably_stale_report(db, monkeypatch):
    old, new = production_pair(db)
    prior = reports.build_report(db.reader, old, "production_mean3")
    monkeypatch.setattr(incremental, "affected_months", lambda formula, outputs, changed: ())
    broken = incremental.build_incremental_report(db.reader, UUID(prior["report_id"]), new)
    complete = reports.build_report(db.reader, new, "production_mean3")
    assert [row["value"] for row in broken["rows"][:2]] == ["2.000000000000", "3.000000000000"]
    assert [row["value"] for row in complete["rows"][:2]] == ["8.000000000000", "9.000000000000"]
    with pytest.raises(AssertionError):
        assert broken["rows"] == complete["rows"]


def test_incremental_generated_revision_trace_matches_independent_full_scan(db):
    old_context, new_context = production_pair(db)
    previous = reports.build_report(db.reader, new_context, "production_mean3")
    first_retained = copy.deepcopy(previous)
    months = ["2025-11-01", "2025-12-01", "2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"]
    states = {
        month: ("value", value)
        for month, value in zip(months, ["1", "20", "3", "4", "5", "6"], strict=True)
    }
    batches = ["B1", "B2"]
    pointer = "S2"
    rng = random.Random(5984)
    for generation in range(3, 15):
        changed_month = months[rng.randrange(len(months))]
        status = rng.choice(["value", "value", "missing", "withdrawn"])
        value = str(rng.randrange(-30, 30)) if status == "value" else None
        states[changed_month] = (status, value)
        raw = copy.deepcopy(G1["revisions"][2])
        raw.update(
            source_revision_key=f"revision-{generation}",
            source_order=20 + generation,
            series_key="production",
            observation_month=changed_month,
            value=value,
            status=status,
            unit={"name": "index", "metadata_version": "v1"},
        )
        batch_id = f"B{generation}"
        raw["occurrences"][0]["batch_id"] = batch_id
        store.import_batch(db.importer, db.dataset_id, batch_id, (revision_from_fixture(raw),))
        batches.append(batch_id)
        snapshot = f"S{generation}"
        store.publish_snapshot(db.publisher, db.dataset_id, snapshot, batches, "e" * 64, pointer)
        pointer = snapshot
        context = store.create_context(
            db.reader,
            db.dataset_id,
            Query(
                "system_snapshot",
                snapshot,
                "synthetic",
                "production",
                tuple(date(2026, m, 1) for m in range(1, 5)),
            ),
        )
        actual = incremental.build_incremental_report(
            db.reader, UUID(previous["report_id"]), context
        )
        complete = reports.build_report(db.reader, context, "production_mean3")
        assert actual["rows"] == complete["rows"]
        for index, row in enumerate(actual["rows"]):
            window = [states[month] for month in months[index : index + 3]]
            unavailable = [state for state, _ in window if state != "value"]
            if unavailable:
                assert row["value"] is None
                assert row["status"] == ("withdrawn" if "withdrawn" in unavailable else "missing")
            else:
                exact = sum(Fraction(value) for _, value in window) / 3
                integer = round(exact * 10**12)
                whole, fraction = divmod(abs(integer), 10**12)
                assert row["value"] == f"{'-' if integer < 0 else ''}{whole}.{fraction:012d}"
        previous = actual
    assert reports.replay_report(db.reader, UUID(first_retained["report_id"])) == first_retained


def test_bundle_is_deterministic_complete_and_detects_changed_payload(db):
    old, _ = paired_contexts(db)
    report = reports.build_report(db.reader, old, "employment_delta")
    report_id = UUID(report["report_id"])
    payload = bundles.export_report_bundle(db.reader, report_id)
    assert payload == bundles.export_report_bundle(db.reader, report_id)
    checked = bundles.verify_bundle(payload)
    assert checked["report"] == report
    assert checked["manifest"]["environment_available"] is True
    assert checked["execution_performed"] is False
    bad = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as original, zipfile.ZipFile(bad, "w") as mutant:
        for name in original.namelist():
            mutant.writestr(name, b"{}" if name == "report.json" else original.read(name))
    with pytest.raises(ContractError) as failure:
        bundles.verify_bundle(bad.getvalue())
    assert failure.value.code == "INVALID_BUNDLE"


def test_bundle_marks_unavailable_old_environment_without_replacing_it(db, monkeypatch):
    old, _ = paired_contexts(db)
    report = reports.build_report(db.reader, old, "employment_delta")
    changed = {**reports.implementation_identity(), "python": "different"}
    monkeypatch.setattr(reports, "implementation_identity", lambda: changed)
    checked = bundles.verify_bundle(
        bundles.export_report_bundle(db.reader, UUID(report["report_id"]))
    )
    assert checked["manifest"]["environment_available"] is False
    assert checked["report"] == report
    assert not any(name.startswith("environment/") for name in checked["manifest"]["files"])


@pytest.mark.parametrize("allow_export", [False, True])
def test_bundle_export_uses_live_source_rights_and_revocation(
    db, tmp_path, monkeypatch, allow_export
):
    monkeypatch.setenv("CHRONICLE_OBJECT_ROOT", str(tmp_path))
    revisions = tuple(revision_from_fixture(item) for item in G1["revisions"][:2])
    store.import_batch(db.importer, db.dataset_id, "B1", revisions)
    uses = ["local_ingest", "local_analysis"] + (["public_export"] if allow_export else [])
    staged = objects.stage_bytes(
        db.publisher,
        db.dataset_id,
        db.dataset_id.encode(),
        {
            "policy_id": db.dataset_id,
            "version": "1",
            "decision_state": "approved_scoped",
            "permitted_uses": uses,
            "scope": "Synthetic export-rights test",
        },
        {"synthetic": True},
    )
    objects.attach_batch_object(db.publisher, db.dataset_id, "B1", staged)
    objects.publish_snapshot(db.publisher, db.dataset_id, "S1", ["B1"], None, uuid4())
    context = store.create_context(
        db.reader,
        db.dataset_id,
        Query("system_snapshot", "S1", "synthetic", "employment", (date(2026, 1, 1),)),
    )
    report = reports.build_report(db.reader, context, "employment_delta")
    report_id = UUID(report["report_id"])
    if allow_export:
        assert (
            bundles.verify_bundle(bundles.export_report_bundle(db.reader, report_id))["status"]
            == "verified"
        )
    else:
        with pytest.raises(ContractError) as failure:
            bundles.export_report_bundle(db.reader, report_id)
        assert failure.value.code == "RIGHTS_DENIED"
    objects.revoke_rights(db.publisher, staged.policy_sha256, "Test live revocation")
    with pytest.raises(ContractError) as failure:
        bundles.export_report_bundle(db.reader, report_id)
    assert failure.value.code == "RIGHTS_DENIED"


def test_comparison_rows_cannot_be_updated_by_owner(db):
    old, new = paired_contexts(db)
    comparison.compare_matrix(db.reader, old, new, "employment_delta")
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        with db.admin.transaction():
            db.admin.execute("SET LOCAL ROLE chronicle_owner")
            db.admin.execute("UPDATE chronicle.comparison_run SET document=document")
