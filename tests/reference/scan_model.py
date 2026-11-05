"""Raw-dictionary full scan of accepted revisions, independent of production types.

Fixtures provide already-evidenced release ordering. This model does not establish
source evidence, import transactions, database isolation or real local history.
"""

from datetime import date


def _day(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return parsed


def _empty(month: str, status: str = "not_available_at_cutoff") -> dict:
    return {
        "observation_month": month,
        "status": status,
        "value": None,
        "source_revision_key": None,
        "source_order": None,
        "unit": None,
        "availability_unknown": status == "unknown_availability",
    }


def select(fixture: dict, query: dict) -> list[dict]:
    """Scan every raw revision for each requested cell in explicit grid order."""
    mode = query["mode"]
    if mode not in {"latest_corrected", "source_as_of", "system_snapshot"}:
        raise ValueError("unknown knowledge mode")
    snapshot_id = query["snapshot_id"]
    if snapshot_id not in fixture["snapshots"]:
        raise ValueError("unknown snapshot")
    batches = set(fixture["snapshots"][snapshot_id])
    cutoff = query.get("cutoff")
    cutoff_day = None
    if mode == "source_as_of":
        if not cutoff:
            raise ValueError("source_as_of requires cutoff")
        if cutoff.get("precision") != "date":
            raise ValueError("unsupported_cutoff_precision")
        cutoff_day = _day(cutoff["exclusive_end_date"])
    elif cutoff is not None:
        raise ValueError("cutoff only applies to source_as_of")

    result = []
    for month in query["months"]:
        if _day(month).day != 1:
            raise ValueError("observation must be a calendar month")
        winner = None
        greatest_unknown_order = None
        for row in fixture["revisions"]:
            if (
                row["source_namespace"] != query["source_namespace"]
                or row["series_key"] != query["series_key"]
                or row["observation_month"] != month
            ):
                continue
            if not any(item["batch_id"] in batches for item in row["occurrences"]):
                continue
            if mode == "source_as_of":
                availability = row["availability"]
                if availability["precision"] == "unknown":
                    order = row["source_order"]
                    if greatest_unknown_order is None or order > greatest_unknown_order:
                        greatest_unknown_order = order
                    continue
                if availability["precision"] != "date":
                    raise ValueError("unsupported source availability precision")
                if _day(availability["date"]) >= cutoff_day:
                    continue
            if winner is None or row["source_order"] > winner["source_order"]:
                winner = row

        # F3: an eligible older value is not authoritative beneath unknown newer evidence.
        if greatest_unknown_order is not None and (
            winner is None or greatest_unknown_order > winner["source_order"]
        ):
            result.append(_empty(month, "unknown_availability"))
            continue
        if winner is None:
            result.append(_empty(month))
            continue
        status = winner["status"]
        if query.get("expected_unit") is not None and winner["unit"] != query["expected_unit"]:
            status = "unit_mismatch"
        result.append(
            {
                "observation_month": month,
                "status": status,
                "value": winner["value"] if status == "value" else None,
                "source_revision_key": winner["source_revision_key"],
                "source_order": winner["source_order"],
                "unit": dict(winner["unit"]),
                "availability_unknown": winner["availability"]["precision"] == "unknown",
            }
        )
    return result
