"""Controlled data/code comparisons with executable, finite calculation versions.

The second shipped version is an explicitly synthetic factor-two demonstration.
It is not a claim that a previously deployed runtime is available, and it cannot
run against non-synthetic namespaces. Both versions execute retained lexical
inputs exactly before canonical rounding in the currently recorded environment.
"""

import hashlib
from datetime import date
from importlib.resources import files
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.types.json import Jsonb

from chronicle import reports
from chronicle.errors import ContractError
from chronicle.numeric import SCALE, canonical_ratio, format_scaled, parse_scaled

CODE_VERSIONS = {
    "fixed-v1": {"factor": 1, "scope": "all", "label": "Fixed monthly formula v1"},
    "synthetic-double-v1": {
        "factor": 2,
        "scope": "synthetic",
        "label": "Synthetic demonstration: double the exact formula before rounding",
    },
}


def _hash(value):
    return hashlib.sha256(reports.canonical_json(value)).hexdigest()


def _controls(document: dict) -> dict:
    keys = (
        "mode",
        "exclusive_end_date",
        "source_namespace",
        "series_key",
        "output_months",
        "expected_unit",
        "formula",
        "formula_version",
        "numeric_policy",
        "unit_policy",
    )
    return {key: document[key] for key in keys}


def _require_same_controls(old: dict, new: dict) -> None:
    if _controls(old) != _controls(new):
        raise ContractError("INCOMPATIBLE_CONTEXT", "Comparison controls must match exactly.")


def _row_difference(old: dict, new: dict) -> dict:
    status = "value"
    value = None
    if old["status"] != "value" or new["status"] != "value":
        status = "incomplete"
    elif old["unit"] != new["unit"]:
        status = "unit_mismatch"
    else:
        value = format_scaled(parse_scaled(new["value"]) - parse_scaled(old["value"]))
    return {
        "observation_month": old["observation_month"],
        "status": status,
        "value": value,
        "unit": old["unit"] if old["unit"] == new["unit"] else None,
        "old_status": old["status"],
        "new_status": new["status"],
    }


def compare_reports(conn: psycopg.Connection, old_report_id: UUID, new_report_id: UUID) -> dict:
    """Subtract saved canonical outputs, never hidden unrounded quantities."""
    old, new = (
        reports.replay_report(conn, old_report_id),
        reports.replay_report(conn, new_report_id),
    )
    _require_same_controls(old, new)
    return {
        "schema": "chronicle.report-comparison.v1",
        "old_report_id": str(old_report_id),
        "new_report_id": str(new_report_id),
        "rows": [
            _row_difference(left, right)
            for left, right in zip(old["rows"], new["rows"], strict=True)
        ],
    }


def execute_version(base_report: dict, code_version: str, run_id: UUID) -> dict:
    if code_version not in CODE_VERSIONS:
        raise ContractError(
            "ENVIRONMENT_UNAVAILABLE", "Requested calculation version is unavailable."
        )
    version = CODE_VERSIONS[code_version]
    if version["scope"] == "synthetic" and base_report["source_namespace"] != "synthetic":
        raise ContractError(
            "SYNTHETIC_VERSION_ONLY", "Demonstration code requires synthetic inputs."
        )
    months = tuple(date.fromisoformat(month) for month in base_report["output_months"])
    # Inputs repeat across overlapping windows; retain exactly one copy per month.
    evidence = {
        item["observation_month"]: {k: v for k, v in item.items() if k != "display"}
        for row in base_report["rows"]
        for item in row["inputs"]
    }
    rows = reports.compose_rows(base_report["formula"], months, list(evidence.values()))
    if version["factor"] != 1:
        for row in rows:
            if row["status"] != "value":
                continue
            values = [parse_scaled(item["value"]) for item in row["inputs"]]
            if base_report["formula"] == "employment_delta":
                row["value"] = format_scaled((values[1] - values[0]) * version["factor"])
            else:
                row["value"] = canonical_ratio(sum(values) * version["factor"], 3 * SCALE)
    return {
        "schema": "chronicle.comparison-child.v1",
        "run_id": str(run_id),
        "base_report_id": base_report["report_id"],
        "context_id": base_report["context_id"],
        "input_context_id": base_report["input_context_id"],
        "code_version": code_version,
        "code_label": version["label"],
        "controls": _controls(base_report),
        "snapshot_id": base_report["snapshot_id"],
        "dataset_id": base_report["dataset_id"],
        "rows": rows,
    }


def decompose_matrix(children: dict) -> list[dict]:
    """Difference of canonical outputs, including an explicit interaction term."""
    aa, ba, ab, bb = (children[key]["rows"] for key in ("old_old", "new_old", "old_new", "new_new"))
    result = []
    for a, b, c, d in zip(aa, ba, ab, bb, strict=True):
        rows = (a, b, c, d)
        if len({row["observation_month"] for row in rows}) != 1:
            raise ContractError("INCOMPATIBLE_CONTEXT", "Child runs have different output grids.")
        compatible = all(row["unit"] == a["unit"] for row in rows)
        complete = all(row["status"] == "value" for row in rows)
        entry = {
            "observation_month": a["observation_month"],
            "status": "value"
            if complete and compatible
            else ("unit_mismatch" if not compatible else "incomplete"),
            "unit": a["unit"] if compatible else None,
            "data_effect": None,
            "code_effect": None,
            "interaction": None,
            "total_change": None,
        }
        if complete and compatible:
            x, y, z, w = (parse_scaled(row["value"]) for row in rows)
            entry.update(
                data_effect=format_scaled(y - x),
                code_effect=format_scaled(z - x),
                interaction=format_scaled(w - y - z + x),
                total_change=format_scaled(w - x),
            )
        result.append(entry)
    return result


def replay_comparison(conn: psycopg.Connection, comparison_id: UUID) -> dict:
    found = conn.execute(
        "SELECT document,document_sha256 FROM chronicle.comparison_run WHERE comparison_id=%s",
        (comparison_id,),
    ).fetchone()
    if found is None:
        raise ContractError("COMPARISON_NOT_FOUND", "No retained comparison has that identity.")
    if _hash(found[0]) != found[1]:
        raise ContractError("COMPARISON_CORRUPT", "Retained comparison digest does not match.")
    return found[0]


def compare_matrix(
    conn: psycopg.Connection,
    old_context_id: UUID,
    new_context_id: UUID,
    formula: str,
    *,
    comparison_id: UUID | None = None,
    output_months: tuple[date, ...] | None = None,
    old_code: str = "fixed-v1",
    new_code: str = "synthetic-double-v1",
) -> dict:
    """Create four server-owned executions atomically under fixed controls."""
    comparison_id = comparison_id or uuid4()
    if any(
        not isinstance(value, UUID) for value in (comparison_id, old_context_id, new_context_id)
    ):
        raise ContractError("INVALID_IDENTITY", "Comparison and context IDs must be UUIDs.")
    if old_code not in CODE_VERSIONS or new_code not in CODE_VERSIONS:
        raise ContractError(
            "ENVIRONMENT_UNAVAILABLE", "Requested calculation version is unavailable."
        )
    if output_months is not None:
        reports.required_months(formula, output_months)
    identity = {
        "runtime": reports.implementation_identity(),
        "comparison_code_sha256": hashlib.sha256(
            files("chronicle").joinpath("comparison.py").read_bytes()
        ).hexdigest(),
    }
    request = {
        "old_context_id": str(old_context_id),
        "new_context_id": str(new_context_id),
        "formula": formula,
        "old_code": old_code,
        "new_code": new_code,
        "output_months": [m.isoformat() for m in output_months]
        if output_months is not None
        else None,
        "implementation": identity,
    }
    request_hash = _hash(request)
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (int.from_bytes(comparison_id.bytes[:8], "big", signed=True),),
        )
        previous = conn.execute(
            "SELECT request_sha256 FROM chronicle.comparison_run WHERE comparison_id=%s",
            (comparison_id,),
        ).fetchone()
        if previous:
            if previous[0] != request_hash:
                raise ContractError("COMPARISON_IDENTITY_CONFLICT", "Comparison identity is bound.")
            return replay_comparison(conn, comparison_id)
        old = reports.build_report(
            conn,
            old_context_id,
            formula,
            output_months=output_months,
            report_id=uuid5(comparison_id, "old-data-base"),
        )
        new = reports.build_report(
            conn,
            new_context_id,
            formula,
            output_months=output_months,
            report_id=uuid5(comparison_id, "new-data-base"),
        )
        _require_same_controls(old, new)
        children = {
            key: execute_version(base, code, uuid5(comparison_id, key))
            for key, base, code in (
                ("old_old", old, old_code),
                ("new_old", new, old_code),
                ("old_new", old, new_code),
                ("new_new", new, new_code),
            )
        }
        document = {
            "schema": "chronicle.four-run-comparison.v1",
            **request,
            "comparison_id": str(comparison_id),
            "children": children,
            "rows": decompose_matrix(children),
            "interpretation": "Controlled arithmetic attribution; no causal economic claim",
        }
        conn.execute(
            "INSERT INTO chronicle.comparison_run(comparison_id,old_context_id,"
            "new_context_id,request_sha256,document_sha256,document) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                comparison_id,
                old_context_id,
                new_context_id,
                request_hash,
                _hash(document),
                Jsonb(document),
            ),
        )
    return document
