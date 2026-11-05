"""Exact rational revision differences, rounded once, beside stored-output differences.

``chronicle.comparison.compare_reports`` subtracts two retained canonical outputs.
Each output was already rounded to 12 places, so that stored-output difference can
carry two roundings. This module keeps that difference unchanged and, beside it,
recomputes each retained row's exact rational value from its retained lexical
inputs, verifies that rounding it once reproduces the retained canonical output,
subtracts the exact rationals and rounds the difference exactly once.

Neither difference replaces the other. The stored-output difference answers "how
far apart are the two published numbers"; the exact difference answers "how far
apart are the two quantities those numbers round". Unavailable, withdrawn, unknown
and unit-mismatched rows never become zero; they keep a null numeric difference.
"""

from fractions import Fraction
from uuid import UUID

import psycopg

from chronicle import comparison, reports
from chronicle.errors import ContractError
from chronicle.numeric import SCALE, canonical_ratio, format_scaled, parse_scaled

SCHEMA = "chronicle.report-comparison-exact.v1"
EXACT_POLICY = "exact-rational-difference-round-once-decimal12-half-even-v1"


def _canonical(value: Fraction) -> str:
    return canonical_ratio(value.numerator, value.denominator)


def exact_row_value(formula: str, row: dict) -> Fraction | None:
    """Return a value row's unrounded rational, after checking its retained output."""
    if formula not in reports.FORMULAS:
        raise ContractError("UNKNOWN_FORMULA", "Select a supported fixed monthly formula.")
    if row["status"] != "value":
        return None
    inputs = row["inputs"]
    if len(inputs) != reports.FORMULAS[formula] + 1 or any(
        item["status"] != "value" for item in inputs
    ):
        raise ContractError(
            "INVALID_EVIDENCE", "A value row must retain its complete formula window."
        )
    scaled = [parse_scaled(item["value"]) for item in inputs]
    if formula == "employment_delta":
        exact = Fraction(scaled[1] - scaled[0], SCALE)
    else:
        exact = Fraction(sum(scaled), len(scaled) * SCALE)
    if _canonical(exact) != row["value"]:
        raise ContractError(
            "EXACT_RECOMPUTATION_MISMATCH",
            "Retained output is not the once-rounded value of its retained inputs.",
        )
    return exact


def _rational(value: Fraction) -> dict:
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def exact_comparison(old: dict, new: dict, stored: dict) -> dict:
    """Put the once-rounded exact difference beside an existing stored comparison."""
    if (stored.get("old_report_id"), stored.get("new_report_id")) != (
        old.get("report_id"),
        new.get("report_id"),
    ):
        raise ContractError(
            "STORED_COMPARISON_MISMATCH", "Stored comparison names different reports."
        )
    comparison._require_same_controls(old, new)
    formula = old["formula"]
    rows = []
    for left, right, saved in zip(old["rows"], new["rows"], stored["rows"], strict=True):
        if comparison._row_difference(left, right) != saved:
            raise ContractError(
                "STORED_COMPARISON_MISMATCH",
                "Stored comparison is not the difference of the retained outputs.",
            )
        before, after = exact_row_value(formula, left), exact_row_value(formula, right)
        difference = None
        if saved["status"] == "value":
            exact = after - before
            rounded = _canonical(exact)
            difference = {
                "exact_difference": rounded,
                "exact_difference_rational": _rational(exact),
                "rounding_gap": format_scaled(parse_scaled(saved["value"]) - parse_scaled(rounded)),
            }
        rows.append(
            {
                "observation_month": saved["observation_month"],
                "status": saved["status"],
                "unit": saved["unit"],
                "old_status": saved["old_status"],
                "new_status": saved["new_status"],
                "stored_output_difference": saved["value"],
                "exact_difference": None,
                "exact_difference_rational": None,
                "rounding_gap": None,
                **(difference or {}),
            }
        )
    return {
        "schema": SCHEMA,
        "old_report_id": stored["old_report_id"],
        "new_report_id": stored["new_report_id"],
        "formula": formula,
        "stored_output_policy": "difference of retained canonical outputs (unchanged)",
        "exact_policy": EXACT_POLICY,
        "rounding_gap_definition": "stored_output_difference - exact_difference",
        "stored_comparison": stored,
        "rows": rows,
    }


def compare_reports_exact(
    conn: psycopg.Connection, old_report_id: UUID, new_report_id: UUID
) -> dict:
    """Run the existing stored comparison unchanged, then add the exact difference."""
    stored = comparison.compare_reports(conn, old_report_id, new_report_id)
    return exact_comparison(
        reports.replay_report(conn, old_report_id),
        reports.replay_report(conn, new_report_id),
        stored,
    )
