"""Report expectations are literal; independent arithmetic uses Fraction."""

import copy
import hashlib
import json
from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest

from chronicle import reports
from chronicle.errors import ContractError

FIXTURES = Path(__file__).parent / "fixtures" / "reports"
ARITHMETIC = json.loads((FIXTURES / "arithmetic-v1.json").read_text())["cases"]
STATUSES = json.loads((FIXTURES / "status-policy-v1.json").read_text())["cases"]


def slots(values):
    months = ["2025-11-01", "2025-12-01", "2026-01-01"][-len(values) :]
    return [
        {
            "observation_month": month,
            "dataset_id": "test",
            "snapshot_id": "S1",
            "source_namespace": "synthetic",
            "series_key": "series",
            "mode": "system_snapshot",
            "exclusive_end_date": None,
            "status": "value",
            "value": value,
            "source_status": "value",
            "unit_compatible": True,
            "unit": {"name": "index", "metadata_version": "v1"},
            "availability_unknown": False,
        }
        for month, value in zip(months, values, strict=True)
    ]


def independent_expected(case):
    values = [Fraction(value) for value in case["values"]]
    value = values[1] - values[0] if case["formula"] == "employment_delta" else sum(values) / 3
    integer = round(value * 10**12)
    magnitude, fraction = divmod(abs(integer), 10**12)
    return f"{'-' if integer < 0 else ''}{magnitude}.{fraction:012d}"


@pytest.mark.parametrize("case", ARITHMETIC, ids=lambda case: case["name"])
def test_exact_formula_matches_literal_and_independent_arithmetic(case):
    evidence = slots(case["values"])
    before = copy.deepcopy(evidence)
    row = reports.compose_rows(case["formula"], (date(2026, 1, 1),), evidence)[0]
    assert row["value"] == case["expected"] == independent_expected(case)
    assert row["status"] == "value"
    assert row["reasons"] == []
    assert evidence == before


@pytest.mark.parametrize("case", STATUSES, ids=lambda case: case["name"])
def test_accepted_status_policy(case):
    assert (
        reports.render_status(
            case["mode"], case["storage_status"], case["source_status"], case["unit_compatible"]
        )
        == case["expected"]
    )


def test_all_formula_calendar_halos_and_output_order():
    assert reports.required_months("production_mean3", (date(2026, 2, 1), date(2026, 1, 1))) == (
        date(2025, 11, 1),
        date(2025, 12, 1),
        date(2026, 1, 1),
        date(2026, 2, 1),
    )
    assert reports.required_months("employment_delta", (date(2026, 1, 1),)) == (
        date(2025, 12, 1),
        date(2026, 1, 1),
    )


@pytest.mark.parametrize(
    "status", ["not_available_at_cutoff", "missing", "withdrawn", "unknown_availability"]
)
def test_unavailable_calendar_input_never_becomes_zero_or_is_skipped(status):
    evidence = slots(["100", "100", "100"])
    evidence[1].update(
        status=status, value=None, source_status=None, unit=None, unit_compatible=None
    )
    row = reports.compose_rows("production_mean3", (date(2026, 1, 1),), evidence)[0]
    assert row["value"] is None
    assert row["status"] == status
    assert len(row["inputs"]) == 3
    assert row["reasons"][0]["observation_month"] == "2025-12-01"


def test_sparse_rows_cannot_substitute_a_different_calendar_month():
    evidence = slots(["100", "100", "100"])
    evidence[1]["observation_month"] = "2025-10-01"
    with pytest.raises(ContractError, match="exact calendar"):
        reports.compose_rows("production_mean3", (date(2026, 1, 1),), evidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("snapshot_id", "S2"),
        ("dataset_id", "foreign"),
        ("exclusive_end_date", "2026-03-16"),
        ("series_key", "different"),
    ],
)
def test_mixed_pinned_scope_rejects_arithmetic(field, value):
    evidence = slots(["90", "100"])
    evidence[0][field] = value
    with pytest.raises(ContractError, match="one pinned scope"):
        reports.compose_rows("employment_delta", (date(2026, 1, 1),), evidence)


@pytest.mark.parametrize("field,value", [("name", "persons"), ("metadata_version", "base2022")])
def test_unit_changes_across_window_cannot_produce_a_number(field, value):
    evidence = slots(["90", "100"])
    evidence[0]["unit"][field] = value
    row = reports.compose_rows("employment_delta", (date(2026, 1, 1),), evidence)[0]
    assert row["value"] is None
    assert row["status"] == "unit_mismatch"
    assert row["warnings"] == ["unit_mismatch"]


@pytest.mark.parametrize("source_status", ["withdrawn", "missing"])
def test_source_failure_display_priority_preserves_null_value(source_status):
    evidence = slots(["90", "100"])
    evidence[1].update(
        status="unit_mismatch", value=None, source_status=source_status, unit_compatible=False
    )
    row = reports.compose_rows("employment_delta", (date(2026, 1, 1),), evidence)[0]
    assert row["value"] is None and row["status"] == source_status
    assert row["inputs"][1]["display"]["storage_status"] == "unit_mismatch"
    assert "unit_mismatch" in row["warnings"]


def test_overflow_does_not_return_partial_rows():
    evidence = slots(["-99999999999999999999999999", "99999999999999999999999999"])
    with pytest.raises(ContractError) as failure:
        reports.compose_rows("employment_delta", (date(2026, 1, 1),), evidence)
    assert failure.value.code == "NUMERIC_OUT_OF_RANGE"


@pytest.mark.parametrize(
    "formula,months,code",
    [
        ("arbitrary", (date(2026, 1, 1),), "UNKNOWN_FORMULA"),
        ("employment_delta", (), "INVALID_GRID"),
        ("employment_delta", (date(1, 1, 1),), "INVALID_GRID"),
        ("production_mean3", (date(1, 2, 1),), "INVALID_GRID"),
        ("employment_delta", (date(2026, 1, 2),), "INVALID_MONTH"),
        ("employment_delta", (date(2026, 1, 1), date(2026, 1, 1)), "INVALID_GRID"),
        ("employment_delta", tuple(date(2026, 1, 1) for _ in range(2401)), "INVALID_GRID"),
    ],
)
def test_invalid_and_unbounded_calendar_requests(formula, months, code):
    with pytest.raises(ContractError) as failure:
        reports.required_months(formula, months)
    assert failure.value.code == code


@pytest.mark.parametrize(
    "original,mutated,index,wrong",
    [
        ("numbers[1] - numbers[0]", "numbers[1] + numbers[0]", 0, "190.000000000000"),
        ("3 * SCALE", "2 * SCALE", 4, "0.500000000000"),
    ],
)
def test_numeric_semantic_mutants_fail_literal_acceptance(original, mutated, index, wrong):
    source = Path(reports.__file__).read_text()
    assert source.count(original) == 1
    namespace = {"__name__": "chronicle_report_mutant"}
    exec(compile(source.replace(original, mutated), "report_mutant.py", "exec"), namespace)
    case = ARITHMETIC[index]
    row = namespace["compose_rows"](case["formula"], (date(2026, 1, 1),), slots(case["values"]))[0]
    assert row["value"] == wrong
    with pytest.raises(AssertionError):
        assert row["value"] == case["expected"]


def test_arithmetic_corpus_and_runtime_lock_are_frozen():
    digest, filename = (FIXTURES / "ARITHMETIC-SHA256SUMS").read_text().split()
    assert hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest() == digest
    root = Path(__file__).parents[1]
    assert (root / "src/chronicle/runtime-lock.sha256").read_text().strip() == hashlib.sha256(
        (root / "uv.lock").read_bytes()
    ).hexdigest()
    assert (root / "src/chronicle/runtime-pyproject.toml").read_bytes() == (
        root / "pyproject.toml"
    ).read_bytes()
    assert (root / "src/chronicle/runtime.lock").read_bytes() == (root / "uv.lock").read_bytes()
