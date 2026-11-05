"""Repeatable real PostgreSQL selection demo over retained synthetic fixtures."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from chronicle import db, store
from tests.postgres.conftest import import_fixture
from tests.support import query_from_fixture


def main():
    scenarios = []
    with (
        db.connect("importer") as importer,
        db.connect("publisher") as publisher,
        db.connect("reader") as reader,
    ):
        for path in sorted((Path(__file__).parents[1] / "fixtures/golden").glob("G*.json")):
            raw = path.read_bytes()
            fixture = json.loads(raw)
            dataset_id = f"synthetic-demo-{fixture['id']}-{hashlib.sha256(raw).hexdigest()[:12]}"
            store.create_dataset(importer, dataset_id)
            fixture_db = SimpleNamespace(
                dataset_id=dataset_id, importer=importer, publisher=publisher, reader=reader
            )
            import_fixture(fixture_db, fixture)
            for query in fixture["queries"]:
                context = store.create_context(reader, dataset_id, query_from_fixture(query))
                scenarios.append(
                    {
                        "fixture": fixture["id"],
                        "query": query["name"],
                        "mode": query["mode"],
                        "snapshot_id": query["snapshot_id"],
                        "cutoff": query["cutoff"],
                        "context_id": str(context),
                        "rows": store.select_context(reader, context),
                    }
                )
    print(
        json.dumps(
            {
                "scope": (
                    "real PostgreSQL selection; synthetic fixtures; no report/API or file protocol"
                ),
                "receipt_clocks": "simulated; not actual historical local knowledge",
                "scenarios": scenarios,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
