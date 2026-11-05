"""Build the descriptive revision report from attributed archive cells.

Run with --check to verify byte-for-byte regeneration without network or database.
"""

import json
import sys
from pathlib import Path

from chronicle.revision_stats import (
    derive_monthly_change,
    derive_trailing_mean,
    revision_statistics,
)

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "data" / "archive" / "releases.json"
OUTPUT = ROOT / "reports" / "revision-statistics-2024-01.json"


def _vintages(prefix: str) -> list[dict]:
    records = json.loads(ARCHIVE.read_text(encoding="utf-8"))["releases"]
    return sorted(
        [
            {
                "vintage_date": row["release_day"],
                "reference_month": row["reference_month"],
                "values": row["values"],
                "provenance": {"source_id": row["source_id"], **row["provenance"]},
            }
            for row in records
            if row["source_id"].startswith(prefix)
        ],
        key=lambda row: row["vintage_date"],
    )


def bls_vintages() -> list[dict]:
    return _vintages("bls-")


def frb_vintages() -> list[dict]:
    return _vintages("frb-")


def _display(vintages: list[dict]) -> list[dict]:
    from chronicle.numeric import canonical_ratio

    shown = []
    for v in vintages:
        values = {
            month: value
            if isinstance(value, str | dict)
            else canonical_ratio(value.numerator, value.denominator)
            for month, value in v["values"].items()
        }
        shown.append({**v, "values": values})
    return shown


def build() -> dict:
    bls, frb = bls_vintages(), frb_vintages()
    series = [
        (
            "bls.ces.total_nonfarm.sa.monthly_change",
            "thousand persons",
            "Derived within each vintage: level(m) - level(m-1). Headline example.",
            derive_monthly_change(bls),
        ),
        (
            "bls.ces.total_nonfarm.sa.level",
            "thousand persons",
            "Table B-1 total nonfarm, seasonally adjusted level cells.",
            bls,
        ),
        (
            "frb.g17.production.sa.index",
            "index, 2017=100, seasonally adjusted",
            "G.17 total industrial production index as parsed by the FRB adapter.",
            frb,
        ),
        (
            "frb.g17.production.sa.mean3",
            "index, 2017=100, seasonally adjusted",
            "Derived within each vintage: exact mean of months m-2..m (Chronicle "
            "production_mean3), kept unrounded until each statistic is rounded once.",
            derive_trailing_mean(frb),
        ),
    ]
    return {
        "schema": "chronicle.revision-statistics-report.v1",
        "method": (
            "Aruoba (2008)-style descriptive revision statistics: revision = later "
            "retained vintage minus first release; per horizon n, mean, sample "
            "standard deviation and direction counts. Horizon h counts later retained "
            "vintages; 'latest' is the last retained vintage."
        ),
        "time_axes": {
            "vintage_date": "publisher release date (source availability, date precision)",
            "observation_month": "period the value describes",
            "local_receipt": "when Chronicle actually received the publication (2026)",
        },
        "series": [
            {
                "series": name,
                "unit": unit,
                "definition": definition,
                "vintages": [
                    {k: v[k] for k in ("vintage_date", "reference_month", "values")}
                    | {"provenance": v["provenance"]}
                    for v in _display(data)
                ],
                **revision_statistics(data),
            }
            for name, unit, definition, data in series
        ],
    }


def render() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        if OUTPUT.read_text(encoding="utf-8") != text:
            print("reports/revision-statistics-2024-01.json is stale", file=sys.stderr)
            return 1
        print("revision statistics report is current")
        return 0
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
