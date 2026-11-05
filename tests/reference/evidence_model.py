"""Independent raw-input full scan for evidence v1; no production imports."""

from datetime import UTC, datetime


def select_evidence(fixture, query, dataset_id):
    batches = set(fixture["snapshots"][query["snapshot_id"]])
    scope = {
        "dataset_id": dataset_id,
        "snapshot_id": query["snapshot_id"],
        "source_namespace": query["source_namespace"],
        "series_key": query["series_key"],
    }

    def record(row):
        identity = {
            **scope,
            "observation_month": row["observation_month"],
            "source_revision_key": row["source_revision_key"],
        }
        occurrences = [
            {
                **identity,
                "batch_id": receipt["batch_id"],
                "received_at": datetime.fromisoformat(receipt["received_at"])
                .astimezone(UTC)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z"),
            }
            for receipt in row["occurrences"]
            if receipt["batch_id"] in batches
        ]
        return {
            **identity,
            "source_order": row["source_order"],
            "availability": row["availability"],
            "source_status": row["status"],
            "lexical_value": row["value"],
            "unit": row["unit"],
            "occurrences": sorted(
                occurrences, key=lambda item: (item["batch_id"].encode(), item["received_at"])
            ),
        }

    results = []
    cutoff = query["cutoff"]["exclusive_end_date"] if query["cutoff"] else None
    for month in query["months"]:
        members = [
            row
            for row in fixture["revisions"]
            if row["source_namespace"] == query["source_namespace"]
            and row["series_key"] == query["series_key"]
            and row["observation_month"] == month
            and any(receipt["batch_id"] in batches for receipt in row["occurrences"])
        ]
        candidates = [
            row
            for row in members
            if cutoff is None
            or (row["availability"]["precision"] == "date" and row["availability"]["date"] < cutoff)
        ]
        winner = max(candidates, key=lambda row: row["source_order"], default=None)
        unknown = max(
            (row for row in members if row["availability"]["precision"] == "unknown"),
            key=lambda row: row["source_order"],
            default=None,
        )
        blocked = (
            cutoff is not None
            and unknown is not None
            and (winner is None or unknown["source_order"] > winner["source_order"])
        )
        compatible = None
        if blocked:
            status, winner = "unknown_availability", None
        elif winner is None:
            status = "not_available_at_cutoff"
        else:
            compatible = query["expected_unit"] is None or query["expected_unit"] == winner["unit"]
            status = winner["status"] if compatible else "unit_mismatch"
        results.append(
            {
                "evidence_schema": "chronicle.evidence.v1",
                **scope,
                "observation_month": month,
                "mode": query["mode"],
                "exclusive_end_date": cutoff,
                "status": status,
                "value": winner["value"] if status == "value" else None,
                "source_status": winner["status"] if winner else None,
                "unit_compatible": compatible,
                "unit": winner["unit"] if winner else None,
                "availability_unknown": blocked
                or (winner is not None and winner["availability"]["precision"] == "unknown"),
                "selected_revision": record(winner) if winner else None,
                "blocking_revision": record(unknown) if blocked else None,
            }
        )
    return results
