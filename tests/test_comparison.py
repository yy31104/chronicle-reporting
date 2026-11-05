"""Frozen four-run counterexample from the design, independent of execution."""

from copy import deepcopy
from datetime import date
from uuid import uuid4

import pytest

from chronicle.comparison import decompose_matrix, execute_version
from chronicle.errors import ContractError
from chronicle.reports import compose_rows
from tests.test_reports import slots


def matrix(values):
    return {
        key: {
            "rows": [
                {
                    "observation_month": "2026-01-01",
                    "status": "value",
                    "value": value,
                    "unit": {"name": "index", "metadata_version": "v1"},
                }
            ]
        }
        for key, value in zip(("old_old", "new_old", "old_new", "new_new"), values, strict=True)
    }


def test_four_run_interaction_counterexample_is_not_additive():
    children = matrix(["10.000000000000", "12.000000000000", "20.000000000000", "24.000000000000"])
    before = deepcopy(children)
    row = decompose_matrix(children)[0]
    assert row["data_effect"] == "2.000000000000"
    assert row["code_effect"] == "10.000000000000"
    assert row["interaction"] == "2.000000000000"
    assert row["total_change"] == "14.000000000000"
    assert children == before


def test_matrix_subtracts_retained_canonical_values():
    children = matrix(["0.333333333333", "0.666666666667", "0.333333333333", "0.666666666667"])
    row = decompose_matrix(children)[0]
    assert row["data_effect"] == "0.333333333334"
    assert row["code_effect"] == row["interaction"] == "0.000000000000"


@pytest.mark.parametrize("kind", ["withdrawn", "unit"])
def test_unavailable_or_incompatible_quadrant_cannot_create_numeric_attribution(kind):
    children = matrix(["10", "12", "20", "24"])
    if kind == "unit":
        children["new_new"]["rows"][0]["unit"]["metadata_version"] = "v2"
    else:
        children["new_new"]["rows"][0].update(status=kind, value=None)
    row = decompose_matrix(children)[0]
    assert row["status"] == ("unit_mismatch" if kind == "unit" else "incomplete")
    assert all(
        row[key] is None for key in ("data_effect", "code_effect", "interaction", "total_change")
    )


def test_matrix_does_not_compare_different_months():
    children = matrix(["10", "12", "20", "24"])
    children["new_new"]["rows"][0]["observation_month"] = "2026-02-01"
    with pytest.raises(ContractError) as failure:
        decompose_matrix(children)
    assert failure.value.code == "INCOMPATIBLE_CONTEXT"


def test_synthetic_code_cannot_be_applied_to_real_source_namespace():
    with pytest.raises(ContractError) as failure:
        execute_version({"source_namespace": "bls"}, "synthetic-double-v1", uuid4())
    assert failure.value.code == "SYNTHETIC_VERSION_ONLY"


def test_second_code_version_executes_before_rounding_not_after():
    evidence = slots(["0", "0", "1"])
    base = {
        "report_id": str(uuid4()),
        "context_id": str(uuid4()),
        "input_context_id": str(uuid4()),
        "dataset_id": "test",
        "snapshot_id": "S1",
        "source_namespace": "synthetic",
        "series_key": "series",
        "mode": "system_snapshot",
        "exclusive_end_date": None,
        "output_months": ["2026-01-01"],
        "expected_unit": None,
        "formula": "production_mean3",
        "formula_version": "production_mean3.v1",
        "numeric_policy": "decimal12-half-even-v1",
        "unit_policy": "exact-name-and-metadata-v1",
        "rows": compose_rows("production_mean3", (date(2026, 1, 1),), evidence),
    }
    assert base["rows"][0]["value"] == "0.333333333333"
    changed = execute_version(base, "synthetic-double-v1", uuid4())
    assert changed["rows"][0]["value"] == "0.666666666667"
