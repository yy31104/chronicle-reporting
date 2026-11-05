"""Observable synthetic contracts checked without importing production code."""

import ast
import copy
import hashlib
import json
import random
import subprocess
import sys
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

import pytest

from tests.reference import rational_metrics as arithmetic
from tests.reference.scan_model import select

GOLDEN = Path(__file__).parent / "fixtures" / "golden"
FIXTURES = [json.loads(path.read_text()) for path in sorted(GOLDEN.glob("G*.json"))]
CASES = [(fixture, query) for fixture in FIXTURES for query in fixture["queries"]]


@pytest.mark.parametrize(
    ("fixture", "query"), CASES, ids=[f"{f['id']}/{q['name']}" for f, q in CASES]
)
def test_full_scan_matches_hand_authored_results(fixture, query):
    assert select(fixture, query) == query["expected"]


def test_hand_authored_fixture_bytes_are_frozen():
    for line in (GOLDEN / "SHA256SUMS").read_text().splitlines():
        expected_digest, filename = line.split()
        assert hashlib.sha256((GOLDEN / filename).read_bytes()).hexdigest() == expected_digest


def test_g1_uses_selected_levels_for_hand_calculated_10_and_13_deltas():
    fixture = FIXTURES[0]
    for query in fixture["queries"]:
        if "expected_january_delta" not in query:
            continue
        rows = select(fixture, query)
        by_month = {row["observation_month"]: row for row in rows}
        actual = arithmetic.delta(by_month["2026-01-01"]["value"], by_month["2025-12-01"]["value"])
        assert actual == query["expected_january_delta"]


def test_receipt_or_fixture_order_does_not_invent_source_chronology():
    rng = random.Random(23)
    for original in FIXTURES:
        fixture = copy.deepcopy(original)
        for _ in range(6):
            rng.shuffle(fixture["revisions"])
            for revision in fixture["revisions"]:
                rng.shuffle(revision["occurrences"])
            for query in fixture["queries"]:
                assert select(fixture, query) == query["expected"]


def test_reference_query_does_not_mutate_raw_evidence():
    fixture = copy.deepcopy(FIXTURES[0])
    before = copy.deepcopy(fixture)
    rows = select(fixture, fixture["queries"][0])
    rows[0]["unit"]["metadata_version"] = "caller-edited"
    assert fixture == before


def test_namespace_and_series_are_both_part_of_the_selection_key():
    fixture = copy.deepcopy(FIXTURES[0])
    original = fixture["queries"][1]
    for key in ("source_namespace", "series_key"):
        query = copy.deepcopy(original)
        query[key] = "not-the-requested-series"
        assert [row["status"] for row in select(fixture, query)] == [
            "not_available_at_cutoff",
            "not_available_at_cutoff",
        ]


@pytest.mark.parametrize(
    "patch",
    [
        {"cutoff": None},
        {"cutoff": {"precision": "instant", "instant": "2026-03-15T12:00:00Z"}},
        {"cutoff": {"precision": "date", "exclusive_end_date": "20260316"}},
        {"mode": "latest"},
        {"snapshot_id": "DOES_NOT_EXIST"},
        {"months": ["2026-01-02"]},
        {"mode": "system_snapshot"},
    ],
)
def test_reference_rejects_unsupported_query_contracts(patch):
    fixture = FIXTURES[0]
    query = {**fixture["queries"][0], **patch}
    with pytest.raises(ValueError):
        select(fixture, query)


def test_f3_mutation_is_killed_by_the_authoritative_status_assertion(tmp_path):
    """Run a real altered module in a subprocess, not a simulated boolean check."""
    original_path = Path(__file__).parent / "reference" / "scan_model.py"
    text = original_path.read_text()
    guard = "if greatest_unknown_order is not None and ("
    assert text.count(guard) == 1
    mutant = tmp_path / "scan_model_no_shadow.py"
    mutant.write_text(text.replace(guard, "if False and ("))
    program = """
import json, runpy, sys
select = runpy.run_path(sys.argv[1])["select"]
fixture = json.load(open(sys.argv[2]))
query = next(q for q in fixture["queries"] if q["name"] == "higher_unknown_blocks_known_value")
actual = select(fixture, query)[0]
assert actual["status"] == "unknown_availability", actual
assert actual["value"] is None, actual
"""
    baseline = subprocess.run(
        [sys.executable, "-I", "-c", program, str(original_path), str(GOLDEN / "G4.json")],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert baseline.returncode == 0, baseline.stderr
    changed = subprocess.run(
        [sys.executable, "-I", "-c", program, str(mutant), str(GOLDEN / "G4.json")],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert changed.returncode == 1
    assert "AssertionError" in changed.stderr
    assert "'status': 'value'" in changed.stderr
    assert "'value': '100'" in changed.stderr


def test_reference_modules_do_not_depend_on_production_code():
    for filename in ("scan_model.py", "rational_metrics.py"):
        tree = ast.parse((Path(__file__).parent / "reference" / filename).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not name.name.startswith("chronicle") for name in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("chronicle")


@pytest.mark.parametrize(
    ("exact", "expected"),
    [
        ("0.0000000000005", "0.000000000000"),
        ("0.0000000000015", "0.000000000002"),
        ("0.0000000000025", "0.000000000002"),
        ("-0.0000000000005", "0.000000000000"),
        ("-0.0000000000015", "-0.000000000002"),
        ("-0.0000000000025", "-0.000000000002"),
        ("0.000000000001499999", "0.000000000001"),
        ("0.000000000001500001", "0.000000000002"),
        ("-0.000000000001499999", "-0.000000000001"),
        ("-0.000000000001500001", "-0.000000000002"),
    ],
)
def test_exact_once_only_rounding_ties_and_neighbors(exact, expected):
    assert arithmetic.canonical(Fraction(exact)) == expected


def test_f2_counterexample_retains_exact_expected_value():
    current = "20000000000000100000000000.000000000003"
    prior = "200.000000000001"
    exact = arithmetic.yoy(current, prior)
    assert exact == "9999999999999999999999900.000000000001"
    # This documents the original defect; it is not the reference calculation.
    with localcontext() as context:
        context.prec = 50
        rounded_twice = (100 * (Decimal(current) / Decimal(prior) - 1)).quantize(
            Decimal("0.000000000001")
        )
    assert str(rounded_twice) == "9999999999999999999999900.000000000002"
    assert str(rounded_twice) != exact


def test_difference_is_of_stored_canonical_outputs():
    old = arithmetic.mean(["102.7", "102.7", "102.6"])
    new = arithmetic.mean(["103.0", "102.7", "102.2"])
    assert old == "102.666666666667"
    assert new == "102.633333333333"
    assert arithmetic.delta(new, old) == "-0.033333333334"
    assert arithmetic.canonical(Fraction(-1, 30)) == "-0.033333333333"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        0,
        1.0,
        "NaN",
        "Infinity",
        "1e2",
        "1,000",
        " 1",
        "1 ",
        "+-1",
        "--1",
        "-+1",
        "++1",
        "0.0000000000001",
        "100000000000000000000000000",
    ],
)
def test_source_decimal_contract_is_not_silently_coerced_or_rounded(bad):
    with pytest.raises(ValueError):
        arithmetic.source(bad)


@pytest.mark.parametrize("sign", ["", "+", "-"])
def test_source_lexical_length_boundary_counts_sign_and_leading_zeros(sign):
    accepted = sign + "0" * (127 - len(sign)) + "1"
    rejected = sign + "0" * (128 - len(sign)) + "1"
    assert len(accepted) == 128
    assert len(rejected) == 129
    assert arithmetic.source(accepted) == (-1 if sign == "-" else 1)
    with pytest.raises(ValueError, match="invalid lexical decimal"):
        arithmetic.source(rejected)


def test_numeric_boundary_zero_denominator_and_overflow():
    largest = "99999999999999999999999999.999999999999"
    assert arithmetic.delta(largest, "0") == largest
    with pytest.raises(ValueError, match="ZERO_DENOMINATOR"):
        arithmetic.yoy("1", "0")
    with pytest.raises(ValueError, match="NUMERIC_OUT_OF_RANGE"):
        arithmetic.delta(largest, "-0.000000000001")
    with pytest.raises(ValueError, match="NUMERIC_OUT_OF_RANGE"):
        arithmetic.yoy(largest, "0.000000000001")
    with pytest.raises(ValueError, match="NUMERIC_OUT_OF_RANGE"):
        arithmetic.yoy("1000000000000000000000000", "0.1")
    assert arithmetic.yoy("90", "100") == "-10.000000000000"


def test_mean_never_skips_a_missing_input_or_replaces_it_with_zero():
    assert arithmetic.mean(["1", "2", "2"]) == "1.666666666667"
    with pytest.raises(ValueError):
        arithmetic.mean(["1", None, "2"])
    with pytest.raises(ValueError):
        arithmetic.mean([])
