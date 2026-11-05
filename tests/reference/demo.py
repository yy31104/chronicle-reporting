"""Run synthetic selection and arithmetic reference models, with no database."""

import json
from pathlib import Path

from tests.reference.rational_metrics import delta
from tests.reference.scan_model import select


def main() -> None:
    golden = Path(__file__).parents[1] / "fixtures" / "golden"
    scenarios = []
    for fixture_id in ("G1", "G2", "G4"):
        fixture = json.loads((golden / f"{fixture_id}.json").read_text())
        for query in fixture["queries"]:
            rows = select(fixture, query)
            scenario = {
                "fixture": fixture_id,
                "query": query["name"],
                "mode": query["mode"],
                "snapshot_id": query["snapshot_id"],
                "cutoff": query["cutoff"],
                "rows": rows,
            }
            if fixture_id == "G1" and len(rows) >= 2:
                scenario["january_delta"] = delta(rows[1]["value"], rows[0]["value"])
            scenarios.append(scenario)
    print(
        json.dumps(
            {
                "scope": "synthetic reference-only demonstration; no production query or database",
                "receipt_clocks": "simulated; not actual historical local knowledge",
                "scenarios": scenarios,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
