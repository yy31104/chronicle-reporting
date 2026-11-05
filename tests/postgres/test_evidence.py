"""Real PostgreSQL evidence, literal anchors and independently scoped mutations."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from chronicle import evidence, store
from chronicle.errors import ContractError
from tests.postgres.conftest import import_fixture
from tests.reference.evidence_model import select_evidence as reference_evidence
from tests.reference.scan_model import select as reference_selection
from tests.support import query_from_fixture

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures"
FIXTURE = json.loads((FIXTURE_DIR / "evidence" / "evidence-v1.json").read_text())
GOLDEN = [json.loads(path.read_text()) for path in sorted((FIXTURE_DIR / "golden").glob("G*.json"))]
GOLDEN_CASES = [(fixture, query) for fixture in GOLDEN for query in fixture["queries"]]


def expected(query, dataset):
    return json.loads(json.dumps(query["expected"]).replace('"$dataset"', json.dumps(dataset)))


@pytest.mark.parametrize("query", FIXTURE["queries"], ids=lambda item: item["name"])
def test_evidence_equals_literal_corpus_and_independent_scan(db, query):
    before = copy.deepcopy(FIXTURE)
    import_fixture(db, FIXTURE)
    context = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    actual = evidence.select_evidence(db.reader, context)
    assert actual == expected(query, db.dataset_id)
    assert actual == reference_evidence(FIXTURE, query, db.dataset_id)
    assert FIXTURE == before


@pytest.mark.parametrize(
    ("fixture", "query"), GOLDEN_CASES, ids=[f"{f['id']}/{q['name']}" for f, q in GOLDEN_CASES]
)
def test_evidence_preserves_all_frozen_selection_semantics(db, fixture, query):
    import_fixture(db, fixture)
    context = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    actual = evidence.select_evidence(db.reader, context)
    assert actual == reference_evidence(fixture, query, db.dataset_id)
    minimal = []
    for slot in actual:
        selected = slot["selected_revision"]
        minimal.append(
            {
                "observation_month": slot["observation_month"],
                "status": slot["status"],
                "value": slot["value"],
                "source_revision_key": selected["source_revision_key"] if selected else None,
                "source_order": selected["source_order"] if selected else None,
                "unit": slot["unit"],
                "availability_unknown": slot["availability_unknown"],
            }
        )
    assert minimal == query["expected"] == reference_selection(fixture, query)
    assert minimal == store.select_context(db.reader, context)


def test_evidence_retains_requested_grid_order_and_is_timezone_independent(db):
    fixture = copy.deepcopy(FIXTURE)
    query = fixture["queries"][4]
    query["months"].reverse()
    query["expected"].reverse()
    import_fixture(db, fixture)
    context = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    db.reader.execute("SET TIME ZONE 'America/New_York'")
    assert evidence.select_evidence(db.reader, context) == expected(query, db.dataset_id)


def test_evidence_unknown_context_is_typed(db):
    with pytest.raises(ContractError) as caught:
        evidence.select_evidence(db.reader, uuid4())
    assert caught.value.code == "CONTEXT_NOT_FOUND"


@pytest.mark.parametrize(
    ("predicate", "higher_foreign_order", "wrong_value", "wrong_order"),
    [
        ("r.dataset_id = c.dataset_id", True, "999", 102),
        ("release.dataset_id = r.dataset_id", True, "+00100.0", 101),
        ("membership.dataset_id = occurrence.dataset_id", False, "103", 2),
        ("occurrence.dataset_id = r.dataset_id", False, "103", 2),
    ],
)
def test_each_dataset_scope_mutation_changes_a_concrete_pinned_evidence_record(
    db, monkeypatch, predicate, higher_foreign_order, wrong_value, wrong_order
):
    # Different membership is essential: identical poisoned snapshots cannot
    # reveal the two inner scope defects found during the PR02 review.
    namespace = f"scope-{uuid4().hex}"
    fixture = json.loads(json.dumps(FIXTURE).replace("evidence-synthetic", namespace))
    import_fixture(db, fixture)
    query = fixture["queries"][0]
    context = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    foreign = copy.deepcopy(fixture)
    foreign["snapshots"] = {"S1": ["B1", "B2"]}
    if higher_foreign_order:
        for row in foreign["revisions"]:
            row["source_order"] += 100
            if row["status"] == "value":
                row["value"] = "999"
    other = SimpleNamespace(**vars(db))
    other.dataset_id = f"poison-{uuid4().hex}"
    store.create_dataset(other.importer, other.dataset_id)
    import_fixture(other, foreign)
    baseline = expected(query, db.dataset_id)
    assert evidence.select_evidence(db.reader, context) == baseline

    assert evidence.SELECT_EVIDENCE_SQL.count(predicate) == 1
    monkeypatch.setattr(
        evidence, "SELECT_EVIDENCE_SQL", evidence.SELECT_EVIDENCE_SQL.replace(predicate, "TRUE", 1)
    )
    actual = evidence.select_evidence(db.reader, context)
    assert actual[0]["status"] == "value"
    assert actual[0]["value"] == wrong_value
    assert actual[0]["selected_revision"]["source_order"] == wrong_order
    # This is a wrong-answer mutation, not an SQL-error-based success.
    with pytest.raises(AssertionError):
        assert actual == baseline


def test_snapshot_scope_mutation_attaches_outside_evidence_and_changes_winner(db, monkeypatch):
    import_fixture(db, FIXTURE)
    query = FIXTURE["queries"][0]
    context = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    baseline = expected(query, db.dataset_id)
    assert evidence.select_evidence(db.reader, context) == baseline
    predicate = "membership.snapshot_id = c.snapshot_id"
    assert evidence.SELECT_EVIDENCE_SQL.count(predicate) == 1
    monkeypatch.setattr(
        evidence, "SELECT_EVIDENCE_SQL", evidence.SELECT_EVIDENCE_SQL.replace(predicate, "TRUE", 1)
    )
    actual = evidence.select_evidence(db.reader, context)
    assert actual[0]["status"] == "withdrawn"
    assert actual[0]["selected_revision"]["source_revision_key"] == "R3"
    assert any(
        receipt["snapshot_id"] != "S1" for receipt in actual[0]["selected_revision"]["occurrences"]
    )
    with pytest.raises(AssertionError):
        assert actual == baseline


def test_unknown_block_mutation_exposes_wrong_authoritative_value(db, monkeypatch):
    import_fixture(db, FIXTURE)
    query = FIXTURE["queries"][2]
    context = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    baseline = expected(query, db.dataset_id)
    assert evidence.select_evidence(db.reader, context) == baseline
    predicate = "unknown.source_order > winner.source_order"
    assert evidence.SELECT_EVIDENCE_SQL.count(predicate) == 1
    monkeypatch.setattr(
        evidence, "SELECT_EVIDENCE_SQL", evidence.SELECT_EVIDENCE_SQL.replace(predicate, "FALSE", 1)
    )
    actual = evidence.select_evidence(db.reader, context)
    assert actual[0]["status"] == "value"
    assert actual[0]["value"] == "+00100.0"
    assert actual[0]["blocking_revision"] is None
    with pytest.raises(AssertionError):
        assert actual == baseline


def _receipt_fixture(count, *, unknown=False):
    from datetime import UTC, datetime, timedelta

    fixture = copy.deepcopy(FIXTURE)
    row = fixture["revisions"][1 if unknown else 0]
    row["occurrences"] = [
        {
            "batch_id": "B1",
            "received_at": (datetime(2026, 2, 16, tzinfo=UTC) + timedelta(microseconds=index))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        }
        for index in range(count)
    ]
    fixture["revisions"] = [row]
    fixture["snapshots"] = {"S1": ["B1"]}
    query = fixture["queries"][0]
    if unknown:
        query["mode"] = "source_as_of"
        query["cutoff"] = {"exclusive_end_date": "2026-03-01"}
    return fixture, query


def test_receipt_limit_accepts_complete_100_receipts_in_deterministic_order(db):
    fixture, query = _receipt_fixture(100)
    import_fixture(db, fixture)
    context = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    actual = evidence.select_evidence(db.reader, context)
    assert actual == reference_evidence(fixture, query, db.dataset_id)
    receipts = actual[0]["selected_revision"]["occurrences"]
    assert len(receipts) == 100
    assert receipts[0]["received_at"] == "2026-02-16T00:00:00.000000Z"
    assert receipts[-1]["received_at"] == "2026-02-16T00:00:00.000099Z"


@pytest.mark.parametrize("unknown", [False, True], ids=["selected", "blocking-unknown"])
def test_receipt_limit_rejects_101_without_partial_or_truncated_evidence(db, unknown):
    fixture, query = _receipt_fixture(101, unknown=unknown)
    import_fixture(db, fixture)
    context = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    with pytest.raises(ContractError) as caught:
        evidence.select_evidence(db.reader, context)
    assert caught.value.code == "EVIDENCE_TOO_LARGE"
    # Legacy scalar selection remains compatible; the stricter bound applies to
    # expanded receipt evidence, not the immutable source or numeric value.
    assert store.select_context(db.reader, context)[0]["status"] == (
        "unknown_availability" if unknown else "value"
    )


def test_foreign_receipts_and_outside_snapshot_receipts_do_not_consume_limit(db):
    fixture, query = _receipt_fixture(100)
    import_fixture(db, fixture)
    context = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    foreign, _ = _receipt_fixture(101)
    other = SimpleNamespace(**vars(db))
    other.dataset_id = f"receipt-poison-{uuid4().hex}"
    store.create_dataset(other.importer, other.dataset_id)
    import_fixture(other, foreign)

    # One extra same-dataset receipt exists, but its batch is outside S1.
    from dataclasses import replace
    from datetime import UTC, datetime

    from chronicle.domain import Occurrence
    from tests.support import revision_from_fixture

    revision = revision_from_fixture(fixture["revisions"][0])
    extra = replace(
        revision, occurrences=(Occurrence("OUTSIDE", datetime(2026, 3, 1, tzinfo=UTC)),)
    )
    store.import_batch(db.importer, db.dataset_id, "OUTSIDE", (extra,))
    actual = evidence.select_evidence(db.reader, context)
    assert actual == reference_evidence(fixture, query, db.dataset_id)
    assert len(actual[0]["selected_revision"]["occurrences"]) == 100
    assert {r["dataset_id"] for r in actual[0]["selected_revision"]["occurrences"]} == {
        db.dataset_id
    }


def test_sql_receipt_probe_is_bounded_before_aggregation(db):
    from psycopg.rows import dict_row

    fixture, query = _receipt_fixture(450)
    import_fixture(db, fixture)
    context = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    # Inspect the actual database projection before the application rejection.
    # This catches moving LIMIT after jsonb_agg, which would allocate all 450.
    with db.reader.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            evidence.SELECT_EVIDENCE_SQL, {"context_id": context, "occurrence_limit": 101}
        )
        rows = cursor.fetchall()
    assert len(rows[0]["selected_revision"]["occurrences"]) == 101
    with pytest.raises(ContractError) as caught:
        evidence.select_evidence(db.reader, context)
    assert caught.value.code == "EVIDENCE_TOO_LARGE"


def test_total_evidence_limit_rejects_before_returning_partial_month_grid(db):
    from datetime import date

    from chronicle.domain import Query

    # All identifiers are valid and every individual revision has 100 receipts,
    # but the complete 18-month evidence body exceeds the real 4 MiB service cap.
    fixture, query = _receipt_fixture(100)
    base = fixture["revisions"][0]
    base["source_namespace"] = "\u6e90" * 200
    base["series_key"] = "\u5217" * 200
    base["source_revision_key"] = "\u4fee" * 200
    for receipt in base["occurrences"]:
        receipt["batch_id"] = "\u6279" * 200
    fixture["revisions"] = [
        {
            **copy.deepcopy(base),
            "observation_month": f"{2026 + index // 12}-{index % 12 + 1:02d}-01",
        }
        for index in range(18)
    ]
    snapshot = "\u7167" * 200
    fixture["snapshots"] = {snapshot: ["\u6279" * 200]}
    import_fixture(db, fixture)
    request = Query(
        "system_snapshot",
        snapshot,
        "\u6e90" * 200,
        "\u5217" * 200,
        tuple(date(2026 + index // 12, index % 12 + 1, 1) for index in range(18)),
    )
    context = store.create_context(db.reader, db.dataset_id, request)
    with pytest.raises(ContractError) as caught:
        evidence.select_evidence(db.reader, context)
    assert caught.value.code == "EVIDENCE_TOO_LARGE"
    assert "4 MiB" in str(caught.value)
    # Rejection cleans up the named cursor/transaction and leaves the immutable
    # scalar query usable; neither database state nor the requested grid changed.
    assert len(store.select_context(db.reader, context)) == 18
    assert db.reader.execute("SELECT count(*) FROM pg_cursors").fetchone() == (0,)
