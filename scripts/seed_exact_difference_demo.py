"""Seed an explicitly synthetic dataset whose stored and exact differences disagree.

The values reuse the shape of the real G.17 January 2024 revision (102.7, 102.7,
102.6 -> 103.0, 102.7, 102.2) in the ``synthetic`` namespace, so a local browser
check can show a one-unit rounding gap without real publisher data. Nothing here is
a publisher observation; receipt clocks are simulated. Run against a local
development database only:

    PYTHONPATH=src uv run --locked python scripts/seed_exact_difference_demo.py
"""

import hashlib
from datetime import UTC, date, datetime

from chronicle import db, store
from chronicle.domain import Occurrence, Revision, SourceAvailability, Unit

DATASET = "chronicle-synthetic-exact-v1"


def _revision(month: str, value: str, *, revised: bool = False) -> Revision:
    return Revision(
        "synthetic",
        "production",
        date.fromisoformat(month),
        "revised" if revised else "initial",
        20 if revised else 10,
        "documented_sequence",
        SourceAvailability("date", date(2026, 3, 15) if revised else date(2026, 2, 15)),
        value,
        "value",
        Unit("index", "v1"),
        (
            Occurrence(
                "B2" if revised else "B1",
                datetime(2026, 3, 15, tzinfo=UTC) if revised else datetime(2026, 2, 15, tzinfo=UTC),
            ),
        ),
    )


def seed(*, connect=db.connect, dataset_id: str = DATASET) -> None:
    manifest = hashlib.sha256(f"{dataset_id}:102.7,102.7,102.6;103.0,102.2".encode()).hexdigest()
    with connect("importer") as conn:
        store.create_dataset(conn, dataset_id)
        store.import_batch(
            conn,
            dataset_id,
            "B1",
            (
                _revision("2025-11-01", "102.7"),
                _revision("2025-12-01", "102.7"),
                _revision("2026-01-01", "102.6"),
            ),
        )
        store.import_batch(
            conn,
            dataset_id,
            "B2",
            (
                _revision("2025-11-01", "103.0", revised=True),
                _revision("2026-01-01", "102.2", revised=True),
            ),
        )
    with connect("publisher") as conn:
        store.publish_snapshot(conn, dataset_id, "S1", ["B1"], manifest, None)
        store.publish_snapshot(conn, dataset_id, "S2", ["B1", "B2"], manifest, "S1")


if __name__ == "__main__":
    seed()
    print(f"seeded synthetic dataset {DATASET} (snapshots S1, S2)")
