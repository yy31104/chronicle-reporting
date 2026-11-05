"""Validate real typed input and admission behavior against explicit counterexamples."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from chronicle.admission import RevisionCatalog
from chronicle.domain import (
    Occurrence,
    Query,
    Snapshot,
    SourceAvailability,
    SourceCutoff,
    Unit,
)
from chronicle.errors import ContractError
from tests.support import query_from_fixture, revision_from_fixture

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def fixture(name):
    return json.loads((GOLDEN / f"{name}.json").read_text())


@pytest.fixture
def revision():
    return revision_from_fixture(fixture("G1")["revisions"][1])


@pytest.mark.parametrize("name", ["G1", "G2", "G3", "G4"])
def test_golden_inputs_satisfy_real_typed_contract(name):
    corpus = fixture(name)
    catalog = RevisionCatalog()
    for row in corpus["revisions"]:
        catalog = catalog.admit(revision_from_fixture(row))
    assert len(catalog.revisions) == len(corpus["revisions"])
    known_batches = {receipt.batch_id for row in catalog.revisions for receipt in row.occurrences}
    for snapshot_id, members in corpus["snapshots"].items():
        snapshot = Snapshot(snapshot_id, frozenset(members))
        assert snapshot.batch_ids <= known_batches
    for raw in corpus["queries"]:
        query = query_from_fixture(raw)
        assert query.snapshot_id in corpus["snapshots"]


@pytest.mark.parametrize(
    ("patch", "error"),
    [
        ({"source_namespace": ""}, "INVALID_IDENTITY"),
        ({"source_revision_key": " padded "}, "INVALID_IDENTITY"),
        ({"observation_month": date(2026, 1, 2)}, "INVALID_MONTH"),
        ({"observation_month": datetime(2026, 1, 1, tzinfo=UTC)}, "INVALID_DATE"),
        ({"source_order": None}, "INVALID_SOURCE_ORDER"),
        ({"source_order": True}, "INVALID_SOURCE_ORDER"),
        ({"source_order": 2**63}, "INVALID_SOURCE_ORDER"),
        ({"order_rule": "download_order"}, "INVALID_REVISION"),
        ({"order_rule": None}, "INVALID_REVISION"),
        ({"status": "missing"}, "INVALID_VALUE_STATUS"),
        ({"status": "withdrawn"}, "INVALID_VALUE_STATUS"),
        ({"value": None}, "INVALID_DECIMAL"),
        ({"value": 100.0}, "INVALID_DECIMAL"),
        ({"occurrences": ()}, "INVALID_OCCURRENCE"),
        ({"occurrences": []}, "INVALID_OCCURRENCE"),
        ({"unit": "persons"}, "INVALID_REVISION"),
        ({"availability": "2026-02-10"}, "INVALID_REVISION"),
    ],
)
def test_invalid_revision_rejected_with_stable_reason(revision, patch, error):
    with pytest.raises(ContractError) as caught:
        replace(revision, **patch)
    assert caught.value.code == error


def test_source_lexical_scale_is_not_erased(revision):
    assert replace(revision, value="+00100.0000").value == "+00100.0000"


def test_reobservation_merges_receipts_without_changing_the_old_catalog(revision):
    old = RevisionCatalog().admit(revision)
    later = Occurrence("later", datetime(2026, 4, 1, tzinfo=UTC))
    new = old.admit(replace(revision, occurrences=(later,)))
    assert len(new.revisions) == 1
    assert new.revisions[0].value == "100"
    assert new.revisions[0].source_order == 10
    assert new.revisions[0].occurrences == revision.occurrences + (later,)
    assert old.revisions == (revision,)
    assert new.admit(revision) == new


@pytest.mark.parametrize("case", fixture("G3")["admission_cases"], ids=lambda case: case["name"])
def test_g3_rejected_candidate_leaves_accepted_catalog_unchanged(case):
    raw = fixture("G3")["revisions"][case["base_revision_index"]]
    accepted = revision_from_fixture(raw)
    old = RevisionCatalog().admit(accepted)
    codes = {
        "conflicting_revision_identity": "REVISION_CONFLICT",
        "unevidenced_source_order": "INVALID_SOURCE_ORDER",
        "ambiguous_source_order": "AMBIGUOUS_SOURCE_ORDER",
    }
    with pytest.raises(ContractError) as caught:
        old.admit(revision_from_fixture(raw | case["patch"]))
    assert caught.value.code == codes[case["expected_error"]]
    assert old.revisions == (accepted,)


def test_release_metadata_is_shared_across_months_and_series(revision):
    catalog = RevisionCatalog().admit(revision)
    other_month = replace(revision, observation_month=date(2025, 12, 1), value="90")
    assert len(catalog.admit(other_month).revisions) == 2
    with pytest.raises(ContractError, match="consistent evidence") as caught:
        catalog.admit(replace(other_month, source_order=11))
    assert caught.value.code == "RELEASE_CONFLICT"


def test_same_day_release_requires_evidence_not_arrival_order(revision):
    initial = replace(
        revision, order_rule="date_ordinal", source_order=revision.availability.date.toordinal()
    )
    second = replace(revision, source_revision_key="second", source_order=initial.source_order + 1)
    for first, later in ((initial, second), (second, initial)):
        with pytest.raises(ContractError) as caught:
            RevisionCatalog().admit(first).admit(later)
        assert caught.value.code == "AMBIGUOUS_SOURCE_ORDER"


def test_evidenced_same_day_sequence_is_accepted(revision):
    second = replace(revision, source_revision_key="second", source_order=11, value="103")
    assert len(RevisionCatalog().admit(revision).admit(second).revisions) == 2


def test_different_day_releases_cannot_mix_incomparable_order_schemes(revision):
    ordinal = replace(
        revision, order_rule="date_ordinal", source_order=revision.availability.date.toordinal()
    )
    newer = replace(
        revision,
        source_revision_key="newer",
        source_order=2,
        availability=SourceAvailability("date", date(2026, 3, 15)),
    )
    for first, later in ((ordinal, newer), (newer, ordinal)):
        with pytest.raises(ContractError) as caught:
            RevisionCatalog().admit(first).admit(later)
        assert caught.value.code == "AMBIGUOUS_SOURCE_ORDER"


def test_namespaces_are_independent(revision):
    unrelated = replace(revision, source_namespace="another", value="999")
    assert len(RevisionCatalog().admit(revision).admit(unrelated).revisions) == 2


def test_date_ordinal_cannot_be_fabricated(revision):
    with pytest.raises(ContractError) as caught:
        replace(revision, order_rule="date_ordinal")
    assert caught.value.code == "INVALID_SOURCE_ORDER"


def test_catalog_constructor_cannot_bypass_identity_checks(revision):
    with pytest.raises(ContractError) as caught:
        RevisionCatalog((revision, replace(revision, value="999")))
    assert caught.value.code == "REVISION_CONFLICT"
    with pytest.raises(ContractError) as caught:
        RevisionCatalog((revision, revision))
    assert caught.value.code == "DUPLICATE_REVISION"


@pytest.mark.parametrize("clock", [datetime(2026, 3, 18), date(2026, 3, 18), "yesterday"])
def test_local_receipt_requires_a_real_aware_instant(clock):
    with pytest.raises(ContractError) as caught:
        Occurrence("B1", clock)
    assert caught.value.code == "INVALID_INSTANT"


def test_local_receipt_normalizes_offset_to_utc():
    receipt = Occurrence("B1", datetime(2026, 3, 18, 12, tzinfo=timezone(timedelta(hours=2))))
    assert receipt.received_at == datetime(2026, 3, 18, 10, tzinfo=UTC)
    assert receipt.received_at.tzinfo is UTC


def test_unknown_availability_never_fabricates_a_date():
    assert SourceAvailability("unknown").date is None
    with pytest.raises(ContractError) as caught:
        SourceAvailability("unknown", date(2026, 3, 15))
    assert caught.value.code == "INVALID_AVAILABILITY"


def test_intraday_precision_is_explicitly_unsupported():
    with pytest.raises(ContractError) as caught:
        SourceCutoff(datetime(2026, 3, 15, 12, tzinfo=UTC))
    assert caught.value.code == "UNSUPPORTED_PRECISION"
    with pytest.raises(ContractError) as caught:
        SourceAvailability("instant", date(2026, 3, 15))
    assert caught.value.code == "UNSUPPORTED_PRECISION"


@pytest.mark.parametrize(
    ("patch", "error"),
    [
        ({"mode": "guess"}, "INVALID_MODE"),
        ({"mode": "source_as_of"}, "CUTOFF_REQUIRED"),
        ({"cutoff": SourceCutoff(date(2026, 3, 16))}, "UNEXPECTED_CUTOFF"),
        ({"snapshot_id": None}, "INVALID_IDENTITY"),
        ({"snapshot_id": ""}, "INVALID_IDENTITY"),
        ({"months": ()}, "INVALID_GRID"),
        ({"months": (date(2026, 1, 1), date(2026, 1, 1))}, "INVALID_GRID"),
        ({"expected_unit": "persons"}, "INVALID_UNIT"),
    ],
)
def test_mode_requirements_are_explicit(patch, error):
    query = Query("system_snapshot", "S1", "synthetic", "employment", (date(2026, 1, 1),))
    with pytest.raises(ContractError) as caught:
        replace(query, **patch)
    assert caught.value.code == error


def test_snapshot_and_revision_are_immutable(revision):
    snapshot = Snapshot("S1", frozenset({"B1"}))
    with pytest.raises(FrozenInstanceError):
        snapshot.batch_ids = frozenset({"B1", "B2"})
    with pytest.raises(FrozenInstanceError):
        revision.value = "999"
    with pytest.raises(ContractError) as caught:
        Snapshot("S1", {"B1"})
    assert caught.value.code == "INVALID_SNAPSHOT"


def test_unit_and_interpretation_version_both_matter():
    assert Unit("thousand_persons", "v1") != Unit("persons", "v1")
    assert Unit("index", "base2017") != Unit("index", "base2022")
