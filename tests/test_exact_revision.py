"""Hand-authored expectations for exact, once-rounded revision differences.

Every expected decimal below was worked out by hand from the lexical inputs before
the production module existed. The documents are written out literally rather than
produced by chronicle.reports.compose_rows, so a wrong formula, a double rounding or
a silently zero-filled gap changes an observable result here.
"""

from copy import deepcopy

import pytest

from chronicle.errors import ContractError
from chronicle.exact_revision import exact_comparison, exact_row_value

INDEX = {"name": "index", "metadata_version": "frb.g17.2017=100.sa.v1"}
PERSONS = {"name": "thousand_persons", "metadata_version": "bls.ces.sa.v1"}


def item(month, value, unit, status="value"):
    return {"observation_month": month, "status": status, "value": value, "unit": unit}


def row(month, status, value, unit, inputs):
    return {
        "observation_month": month,
        "status": status,
        "value": value,
        "unit": unit,
        "inputs": inputs,
    }


def document(report_id, formula, unit, rows):
    return {
        "report_id": report_id,
        "mode": "system_snapshot",
        "exclusive_end_date": None,
        "source_namespace": "frb.g17.archive",
        "series_key": "production",
        "output_months": [r["observation_month"] for r in rows],
        "expected_unit": unit,
        "formula": formula,
        "formula_version": formula + ".v1",
        "numeric_policy": "decimal12-half-even-v1",
        "unit_policy": "exact-name-and-metadata-v1",
        "rows": rows,
    }


def stored(old, new, rows):
    return {
        "schema": "chronicle.report-comparison.v1",
        "old_report_id": old["report_id"],
        "new_report_id": new["report_id"],
        "rows": rows,
    }


def stored_row(month, status, value, unit, old_status="value", new_status="value"):
    return {
        "observation_month": month,
        "status": status,
        "value": value,
        "unit": unit,
        "old_status": old_status,
        "new_status": new_status,
    }


def frb_pair():
    """G.17 vintages 2024-02-15 and 2024-03-15, January 2024 three-month mean."""
    old = document(
        "old-frb",
        "production_mean3",
        INDEX,
        [
            row(
                "2024-01-01",
                "value",
                "102.666666666667",
                INDEX,
                [
                    item("2023-11-01", "102.7", INDEX),
                    item("2023-12-01", "102.7", INDEX),
                    item("2024-01-01", "102.6", INDEX),
                ],
            )
        ],
    )
    new = document(
        "new-frb",
        "production_mean3",
        INDEX,
        [
            row(
                "2024-01-01",
                "value",
                "102.633333333333",
                INDEX,
                [
                    item("2023-11-01", "103.0", INDEX),
                    item("2023-12-01", "102.7", INDEX),
                    item("2024-01-01", "102.2", INDEX),
                ],
            )
        ],
    )
    saved = stored(old, new, [stored_row("2024-01-01", "value", "-0.033333333334", INDEX)])
    return old, new, saved


def bls_pair():
    """Employment Situation 2024-02-02 and 2024-03-08: January change 353 -> 229."""
    old = document(
        "old-bls",
        "employment_delta",
        PERSONS,
        [
            row(
                "2024-01-01",
                "value",
                "353.000000000000",
                PERSONS,
                [item("2023-12-01", "157347", PERSONS), item("2024-01-01", "157700", PERSONS)],
            )
        ],
    )
    new = document(
        "new-bls",
        "employment_delta",
        PERSONS,
        [
            row(
                "2024-01-01",
                "value",
                "229.000000000000",
                PERSONS,
                [item("2023-12-01", "157304", PERSONS), item("2024-01-01", "157533", PERSONS)],
            )
        ],
    )
    saved = stored(old, new, [stored_row("2024-01-01", "value", "-124.000000000000", PERSONS)])
    return old, new, saved


def test_real_frb_mean_difference_rounds_once_and_exposes_the_one_unit_gap():
    # 308.0/3 - 307.9/3 = -0.1/3 = -1/30 = -0.0333...; once-rounded: -0.033333333333.
    # Stored outputs 102.633333333333 - 102.666666666667 = -0.033333333334.
    old, new, saved = frb_pair()
    result = exact_comparison(old, new, saved)
    assert result["schema"] == "chronicle.report-comparison-exact.v1"
    assert result["stored_comparison"] == saved
    (only,) = result["rows"]
    assert only["status"] == "value"
    assert only["stored_output_difference"] == "-0.033333333334"
    assert only["exact_difference"] == "-0.033333333333"
    assert only["exact_difference_rational"] == {"numerator": "-1", "denominator": "30"}
    assert only["rounding_gap"] == "-0.000000000001"
    assert only["unit"] == INDEX


def test_real_bls_january_change_353_to_229_has_no_rounding_gap():
    old, new, saved = bls_pair()
    (only,) = exact_comparison(old, new, saved)["rows"]
    assert only["stored_output_difference"] == "-124.000000000000"
    assert only["exact_difference"] == "-124.000000000000"
    assert only["exact_difference_rational"] == {"numerator": "-124", "denominator": "1"}
    assert only["rounding_gap"] == "0.000000000000"


def test_positive_gap_when_old_rounds_down_and_new_rounds_up():
    # old mean 1e-12/3 -> 0.000000000000; new mean 2e-12/3 -> 0.000000000001.
    # stored difference 0.000000000001; exact difference 1e-12/3 -> 0.000000000000.
    old, new, _ = frb_pair()
    old["rows"][0]["value"] = "0.000000000000"
    old["rows"][0]["inputs"] = [
        item("2023-11-01", "0", INDEX),
        item("2023-12-01", "0", INDEX),
        item("2024-01-01", "0.000000000001", INDEX),
    ]
    new["rows"][0]["value"] = "0.000000000001"
    new["rows"][0]["inputs"] = [
        item("2023-11-01", "0", INDEX),
        item("2023-12-01", "0", INDEX),
        item("2024-01-01", "0.000000000002", INDEX),
    ]
    saved = stored(old, new, [stored_row("2024-01-01", "value", "0.000000000001", INDEX)])
    (only,) = exact_comparison(old, new, saved)["rows"]
    assert only["stored_output_difference"] == "0.000000000001"
    assert only["exact_difference"] == "0.000000000000"
    assert only["exact_difference_rational"] == {"numerator": "1", "denominator": "3000000000000"}
    assert only["rounding_gap"] == "0.000000000001"


def test_negative_exact_difference_rounds_to_nearest_once():
    # old sum 0.000000000002 (mean 2/3e-12 -> 1e-12); new sum 0 (mean 0).
    # exact -2/3e-12 rounds to -0.000000000001; stored 0 - 1e-12 = -0.000000000001; no gap.
    old, new, _ = frb_pair()
    old["rows"][0]["value"] = "0.000000000001"
    old["rows"][0]["inputs"] = [
        item("2023-11-01", "0.000000000001", INDEX),
        item("2023-12-01", "0.000000000001", INDEX),
        item("2024-01-01", "0", INDEX),
    ]
    new["rows"][0]["value"] = "0.000000000000"
    new["rows"][0]["inputs"] = [
        item("2023-11-01", "0", INDEX),
        item("2023-12-01", "0", INDEX),
        item("2024-01-01", "0", INDEX),
    ]
    saved = stored(old, new, [stored_row("2024-01-01", "value", "-0.000000000001", INDEX)])
    (only,) = exact_comparison(old, new, saved)["rows"]
    assert only["exact_difference"] == "-0.000000000001"
    assert only["exact_difference_rational"] == {"numerator": "-1", "denominator": "1500000000000"}
    assert only["rounding_gap"] == "0.000000000000"


@pytest.mark.parametrize("status", ["withdrawn", "missing", "unknown_availability"])
def test_unavailable_side_never_becomes_zero(status):
    old, new, _ = frb_pair()
    new["rows"][0].update(status=status, value=None)
    new["rows"][0]["inputs"][2].update(status=status, value=None)
    saved = stored(
        old,
        new,
        [stored_row("2024-01-01", "incomplete", None, INDEX, new_status=status)],
    )
    (only,) = exact_comparison(old, new, saved)["rows"]
    assert only["status"] == "incomplete"
    assert only["stored_output_difference"] is None
    assert only["exact_difference"] is None
    assert only["exact_difference_rational"] is None
    assert only["rounding_gap"] is None
    assert only["new_status"] == status


def test_unit_mismatch_has_no_numeric_difference():
    old, new, _ = frb_pair()
    other = {"name": "index", "metadata_version": "frb.g17.2012=100.sa.v1"}
    new["rows"][0]["unit"] = other
    saved = stored(old, new, [stored_row("2024-01-01", "unit_mismatch", None, None)])
    (only,) = exact_comparison(old, new, saved)["rows"]
    assert only["status"] == "unit_mismatch"
    assert only["exact_difference"] is None and only["unit"] is None


def test_stored_output_that_is_not_the_once_rounded_inputs_fails_closed():
    old, new, saved = frb_pair()
    old["rows"][0]["value"] = "102.666666666666"  # truncated, not half-even rounded
    # Keep the stored comparison consistent with the tampered output, so only the
    # recomputation from retained inputs can notice: 102.633333333333 - 102.666666666666.
    saved["rows"][0]["value"] = "-0.033333333333"
    with pytest.raises(ContractError) as failure:
        exact_comparison(old, new, saved)
    assert failure.value.code == "EXACT_RECOMPUTATION_MISMATCH"


def test_stored_comparison_must_equal_subtraction_of_stored_outputs():
    old, new, saved = frb_pair()
    saved["rows"][0]["value"] = "-0.033333333333"  # silently "fixed" stored difference
    with pytest.raises(ContractError) as failure:
        exact_comparison(old, new, saved)
    assert failure.value.code == "STORED_COMPARISON_MISMATCH"


def test_stored_comparison_must_name_the_same_reports():
    old, new, saved = frb_pair()
    saved["old_report_id"] = "someone-else"
    with pytest.raises(ContractError) as failure:
        exact_comparison(old, new, saved)
    assert failure.value.code == "STORED_COMPARISON_MISMATCH"


def test_controls_must_match():
    old, new, saved = frb_pair()
    new["series_key"] = "other"
    with pytest.raises(ContractError) as failure:
        exact_comparison(old, new, saved)
    assert failure.value.code == "INCOMPATIBLE_CONTEXT"


def test_window_must_have_exact_formula_length():
    old, new, saved = frb_pair()
    old["rows"][0]["inputs"].pop(0)
    with pytest.raises(ContractError) as failure:
        exact_comparison(old, new, saved)
    assert failure.value.code == "INVALID_EVIDENCE"


def test_exact_row_value_returns_unrounded_rational():
    old, _, _ = frb_pair()
    value = exact_row_value("production_mean3", old["rows"][0])
    assert (value.numerator, value.denominator) == (308, 3)  # 308.0 / 3


def test_inputs_are_not_mutated():
    old, new, saved = frb_pair()
    before = deepcopy((old, new, saved))
    exact_comparison(old, new, saved)
    assert (old, new, saved) == before
