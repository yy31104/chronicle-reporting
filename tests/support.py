"""Adapt fixture records to production types, never compute expected answers."""

from datetime import date, datetime

from chronicle.domain import Occurrence, Query, Revision, SourceAvailability, SourceCutoff, Unit


def revision_from_fixture(row):
    availability = row["availability"]
    return Revision(
        source_namespace=row["source_namespace"],
        series_key=row["series_key"],
        observation_month=date.fromisoformat(row["observation_month"]),
        source_revision_key=row["source_revision_key"],
        source_order=row["source_order"],
        order_rule=row["order_rule"],
        availability=SourceAvailability(
            availability["precision"],
            date.fromisoformat(availability["date"]) if availability["date"] else None,
        ),
        value=row["value"],
        status=row["status"],
        unit=Unit(**row["unit"]),
        occurrences=tuple(
            Occurrence(item["batch_id"], datetime.fromisoformat(item["received_at"]))
            for item in row["occurrences"]
        ),
    )


def query_from_fixture(row):
    return Query(
        mode=row["mode"],
        snapshot_id=row["snapshot_id"],
        source_namespace=row["source_namespace"],
        series_key=row["series_key"],
        months=tuple(date.fromisoformat(month) for month in row["months"]),
        cutoff=SourceCutoff(date.fromisoformat(row["cutoff"]["exclusive_end_date"]))
        if row["cutoff"]
        else None,
        expected_unit=Unit(**row["expected_unit"]) if row["expected_unit"] else None,
    )
