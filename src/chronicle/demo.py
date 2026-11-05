"""Packaged synthetic end-to-end demonstration through the production store."""

import hashlib
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, uuid5

from chronicle import db, store
from chronicle.domain import Occurrence, Query, Revision, SourceAvailability, SourceCutoff, Unit
from chronicle.reports import build_report, replay_report

DATASET = "chronicle-synthetic-v1"
MANIFEST = hashlib.sha256(b"chronicle-synthetic-v1:90,100,103;102.7,102.7,102.6").hexdigest()


def _revision(series, month, value, *, revised=False):
    return Revision(
        "synthetic",
        series,
        date.fromisoformat(month),
        "revised" if revised else "initial",
        20 if revised else 10,
        "documented_sequence",
        SourceAvailability("date", date(2026, 3, 15) if revised else date(2026, 2, 10)),
        value,
        "value",
        Unit("thousand_persons" if series == "employment" else "index", "v1"),
        (
            Occurrence(
                "B2" if revised else "B1",
                datetime(2026, 3, 15, tzinfo=UTC) if revised else datetime(2026, 2, 10, tzinfo=UTC),
            ),
        ),
    )


def seed(*, connect=None, dataset_id=DATASET):
    connect = connect or db.connect
    with connect("importer") as conn:
        store.create_dataset(conn, dataset_id)
        store.import_batch(
            conn,
            dataset_id,
            "B1",
            (
                _revision("employment", "2025-12-01", "90"),
                _revision("employment", "2026-01-01", "100"),
                _revision("production", "2025-11-01", "102.7"),
                _revision("production", "2025-12-01", "102.7"),
                _revision("production", "2026-01-01", "102.6"),
            ),
        )
        store.import_batch(
            conn, dataset_id, "B2", (_revision("employment", "2026-01-01", "103", revised=True),)
        )
    with connect("publisher") as conn:
        store.publish_snapshot(conn, dataset_id, "S1", ["B1"], MANIFEST, None)
        store.publish_snapshot(conn, dataset_id, "S2", ["B1", "B2"], MANIFEST, "S1")


def run(*, connect=None, dataset_id=DATASET):
    connect = connect or db.connect
    seed(connect=connect, dataset_id=dataset_id)
    documents = []
    for name, mode, snapshot, cutoff, series, formula in (
        ("before", "source_as_of", "S2", date(2026, 3, 15), "employment", "employment_delta"),
        ("after", "source_as_of", "S2", date(2026, 3, 16), "employment", "employment_delta"),
        ("old", "system_snapshot", "S1", None, "employment", "employment_delta"),
        ("mean", "latest_corrected", "S2", None, "production", "production_mean3"),
    ):
        with connect("reader") as conn:
            context_id = uuid5(NAMESPACE_URL, f"{dataset_id}:{name}:context")
            query = Query(
                mode,
                snapshot,
                "synthetic",
                series,
                (date(2026, 1, 1),),
                SourceCutoff(cutoff) if cutoff else None,
                Unit("thousand_persons" if series == "employment" else "index", "v1"),
            )
            store.create_context(conn, dataset_id, query, context_id)
            # Code identity changes create a new result; retained old reports remain replayable.
            document = build_report(conn, context_id, formula)
            assert replay_report(conn, document["report_id"]) == document
            documents.append({"case": name, "report": document})
    return {
        "synthetic": True,
        "receipt_clocks": "simulated",
        "dataset_id": dataset_id,
        "reports": documents,
    }
