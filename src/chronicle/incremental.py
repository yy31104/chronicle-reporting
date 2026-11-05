"""Static calendar footprints and conservative arithmetic reuse across snapshots.

Every new input slot and its provenance is read from the new pinned context.
Only canonical numeric results whose complete dependency selection is unchanged
are reused. This implementation reduces formula evaluations, not database reads;
performance benefits require measurement and are not inferred from reuse counts.
"""

from datetime import date
from uuid import UUID

import psycopg

from chronicle import reports
from chronicle.errors import ContractError


def affected_months(
    formula: str,
    output_months: tuple[date, ...],
    changed_months: set[date],
) -> tuple[date, ...]:
    reports.required_months(formula, output_months)
    return tuple(
        month
        for month in output_months
        if any(
            reports.shift_month(month, offset) in changed_months
            for offset in range(-reports.FORMULAS[formula], 1)
        )
    )


def _signature(slot: dict) -> dict:
    keys = ("status", "value", "source_status", "unit_compatible", "unit", "availability_unknown")
    result = {key: slot[key] for key in keys}
    # Changes to receipt lists refresh evidence, but cannot alter arithmetic.
    for key in ("selected_revision", "blocking_revision"):
        revision = slot.get(key)
        result[key] = (
            {
                k: v
                for k, v in revision.items()
                if k not in ("dataset_id", "snapshot_id", "occurrences")
            }
            if revision
            else None
        )
    return result


def selection_changes(old_evidence: list[dict], new_evidence: list[dict]) -> set[date]:
    old = {item["observation_month"]: _signature(item) for item in old_evidence}
    new = {item["observation_month"]: _signature(item) for item in new_evidence}
    # Missing slots are actual dependencies. A late backfill must invalidate them.
    return {
        date.fromisoformat(month)
        for month in old.keys() | new.keys()
        if old.get(month) != new.get(month)
    }


def build_incremental_report(
    conn: psycopg.Connection,
    old_report_id: UUID,
    new_context_id: UUID,
    *,
    report_id: UUID | None = None,
) -> dict:
    old = reports.replay_report(conn, old_report_id)
    output_months = tuple(date.fromisoformat(month) for month in old["output_months"])
    old_inputs = {
        item["observation_month"]: {k: v for k, v in item.items() if k != "display"}
        for row in old["rows"]
        for item in row["inputs"]
    }

    def build_rows(context, formula, months, evidence):
        if context["dataset_id"] != old["dataset_id"]:
            raise ContractError("INCOMPATIBLE_CONTEXT", "Incremental reports require one dataset.")
        cutoff = (
            context["exclusive_end_date"].isoformat() if context["exclusive_end_date"] else None
        )
        if (context["mode"], cutoff, context["source_namespace"], context["series_key"]) != (
            old["mode"],
            old["exclusive_end_date"],
            old["source_namespace"],
            old["series_key"],
        ):
            raise ContractError("INCOMPATIBLE_CONTEXT", "Incremental report controls changed.")
        fallback = None
        if old["implementation"] != reports.implementation_identity():
            fallback = "implementation_changed"
        elif old["report_schema"] != reports.REPORT_SCHEMA or old["evidence_schema"] != (
            "chronicle.evidence.v1"
        ):
            fallback = "schema_changed"
        unit = (
            {
                "name": context["expected_unit_name"],
                "metadata_version": context["expected_metadata_version"],
            }
            if context["expected_unit_name"]
            else None
        )
        if unit != old["expected_unit"]:
            fallback = "unit_policy_changed"
        for item in evidence:
            prior = old_inputs.get(item["observation_month"])
            if (
                prior
                and prior["unit"] is not None
                and item["unit"] is not None
                and prior["unit"] != item["unit"]
            ):
                fallback = "unit_metadata_changed"
        changed = selection_changes(list(old_inputs.values()), evidence)
        affected = affected_months(formula, months, changed)
        if fallback:
            rows = reports.compose_rows(formula, months, evidence)
            return rows, {
                "strategy": "full",
                "fallback_reason": fallback,
                "changed_input_months": sorted(m.isoformat() for m in changed),
                "recomputed_months": [m.isoformat() for m in months],
                "reused_months": [],
            }
        invalidated = {month.isoformat() for month in affected}
        reused = {
            row["observation_month"]: row["value"]
            for row in old["rows"]
            if row["observation_month"] not in invalidated and row["status"] == "value"
        }
        rows = reports.compose_rows(formula, months, evidence, _reused_values=reused)
        return rows, {
            "strategy": "incremental",
            "fallback_reason": None,
            "changed_input_months": sorted(m.isoformat() for m in changed),
            "recomputed_months": [m.isoformat() for m in months if m.isoformat() not in reused],
            "reused_months": [m.isoformat() for m in months if m.isoformat() in reused],
        }

    return reports._build_report(
        conn,
        new_context_id,
        old["formula"],
        output_months=output_months,
        report_id=report_id,
        row_builder=build_rows,
        execution={"algorithm": "calendar-footprint-reuse-v1", "old_report_id": str(old_report_id)},
    )
