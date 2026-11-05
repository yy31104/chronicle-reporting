"""Real PostgreSQL selection compared with frozen answers and a raw full scan."""

import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from chronicle import store
from tests.postgres.conftest import import_fixture
from tests.reference.scan_model import select
from tests.support import query_from_fixture

GOLDEN = Path(__file__).parents[1] / "fixtures" / "golden"
FIXTURES = [json.loads(path.read_text()) for path in sorted(GOLDEN.glob("G*.json"))]
CASES = [(fixture, query) for fixture in FIXTURES for query in fixture["queries"]]


@pytest.mark.parametrize(
    ("fixture", "query"), CASES, ids=[f"{f['id']}/{q['name']}" for f, q in CASES]
)
def test_sql_matches_literal_expectations_and_independent_scan(db, fixture, query):
    before = copy.deepcopy(fixture)
    import_fixture(db, fixture)
    context_id = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    actual = store.select_context(db.reader, context_id)
    assert actual == query["expected"]
    assert actual == select(fixture, query)
    assert fixture == before


def test_requested_grid_order_survives_sql_execution(db):
    fixture = FIXTURES[0]
    import_fixture(db, fixture)
    query = copy.deepcopy(fixture["queries"][0])
    query["months"].reverse()
    context_id = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    assert store.select_context(db.reader, context_id) == list(reversed(query["expected"]))
    assert store.select_context(db.reader, context_id) == select(fixture, query)


def test_namespace_and_series_both_restrict_sql_candidates(db):
    fixture = FIXTURES[0]
    import_fixture(db, fixture)
    for field in ("source_namespace", "series_key"):
        query = copy.deepcopy(fixture["queries"][1])
        query[field] = "unrelated"
        context_id = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
        actual = store.select_context(db.reader, context_id)
        assert actual == select(fixture, query)
        assert [row["status"] for row in actual] == [
            "not_available_at_cutoff",
            "not_available_at_cutoff",
        ]
        assert all(row["value"] is None for row in actual)


def _import_foreign_dataset(db, fixture):
    other = SimpleNamespace(**vars(db))
    other.dataset_id = f"poison-{uuid4().hex}"
    store.create_dataset(other.importer, other.dataset_id)
    import_fixture(other, fixture)
    return other


def test_same_source_and_snapshot_identifiers_cannot_cross_dataset_boundaries(db):
    fixture = FIXTURES[0]
    untouched = copy.deepcopy(fixture)
    import_fixture(db, fixture)
    query = next(item for item in fixture["queries"] if item["name"] == "immutable_old_snapshot")
    first_context = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    assert store.select_context(db.reader, first_context) == query["expected"]

    poisoned = copy.deepcopy(fixture)
    poisoned["snapshots"] = {"S1": ["B1", "B2"]}
    other = _import_foreign_dataset(db, poisoned)
    second_context = store.create_context(other.reader, other.dataset_id, query_from_fixture(query))

    # Same named snapshot, different membership: foreign B2 must not add 103 to
    # the original S1, whose frozen January answer is still 100.
    assert store.select_context(db.reader, first_context) == query["expected"]
    assert [row["value"] for row in store.select_context(db.reader, second_context)] == [
        "90",
        "103",
    ]
    assert fixture == untouched


@pytest.mark.parametrize(
    ("predicate", "foreign_values_win", "wrong_values"),
    [
        pytest.param(
            "membership.dataset_id = occurrence.dataset_id",
            False,
            ["90", "103"],
            id="membership-occurrence-scope",
        ),
        pytest.param(
            "occurrence.dataset_id = r.dataset_id",
            False,
            ["90", "103"],
            id="occurrence-revision-scope",
        ),
        pytest.param(
            "r.dataset_id = c.dataset_id",
            True,
            ["999", "999"],
            id="revision-context-scope",
        ),
    ],
)
def test_each_dataset_scope_mutation_breaks_the_pinned_answer(
    db, monkeypatch, predicate, foreign_values_win, wrong_values
):
    fixture = copy.deepcopy(FIXTURES[0])
    # Share this namespace only within the two test datasets, so removing the
    # outer guard cannot accidentally select another test's retained inputs.
    namespace = f"scope-{uuid4().hex}"
    for revision in fixture["revisions"]:
        revision["source_namespace"] = namespace
    for request in fixture["queries"]:
        request["source_namespace"] = namespace
    untouched = copy.deepcopy(fixture)
    import_fixture(db, fixture)
    query = next(item for item in fixture["queries"] if item["name"] == "immutable_old_snapshot")
    context_id = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))

    poisoned = copy.deepcopy(fixture)
    poisoned["snapshots"] = {"S1": ["B1", "B2"]}
    if foreign_values_win:
        # Distinct higher evidenced orders make foreign winners deterministic;
        # the regression must not depend on unspecified SQL tie-breaking.
        for revision in poisoned["revisions"]:
            revision["source_order"] += 100
            revision["value"] = "999"
    _import_foreign_dataset(db, poisoned)
    assert store.select_context(db.reader, context_id) == query["expected"]
    assert store.select_context(db.reader, context_id) == select(fixture, query)

    count = store.SELECT_SQL.count(predicate)
    assert count == 1, "mutation must replace exactly one dataset-scope predicate"
    monkeypatch.setattr(store, "SELECT_SQL", store.SELECT_SQL.replace(predicate, "TRUE", 1))
    actual = store.select_context(db.reader, context_id)
    assert [row["status"] for row in actual] == ["value", "value"]
    assert [row["value"] for row in actual] == wrong_values
    with pytest.raises(AssertionError):
        assert actual == query["expected"]
    assert fixture == untouched


def test_f3_sql_mutation_returns_the_detectably_wrong_authoritative_value(db, monkeypatch):
    fixture = FIXTURES[3]
    import_fixture(db, fixture)
    query = next(
        item for item in fixture["queries"] if item["name"] == "higher_unknown_blocks_known_value"
    )
    context_id = store.create_context(db.reader, db.dataset_id, query_from_fixture(query))
    assert store.select_context(db.reader, context_id) == query["expected"]

    mutant, count = re.subn(
        r"/\* F3_UNKNOWN_SHADOW_BEGIN \*/.*?/\* F3_UNKNOWN_SHADOW_END \*/",
        "FALSE",
        store.SELECT_SQL,
        flags=re.DOTALL,
    )
    assert count == 1, "mutation must replace exactly the SQL uncertainty guard"
    monkeypatch.setattr(store, "SELECT_SQL", mutant)
    actual = store.select_context(db.reader, context_id)
    assert actual[0]["status"] == "value"
    assert actual[0]["value"] == "100"
    with pytest.raises(AssertionError):
        assert actual == query["expected"]
    with pytest.raises(AssertionError):
        assert actual == select(fixture, query)
