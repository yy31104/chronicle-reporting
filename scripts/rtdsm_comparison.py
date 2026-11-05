"""Compare retained BLS and FRB values with the Philadelphia Fed Real-Time Data Set.

The Real-Time Data Set for Macroeconomists (RTDSM) keeps one column per monthly
vintage. This script reads two of its monthly-vintage workbooks:

- employMvMd.xlsx (EMPLOY, nonfarm payroll employment, thousands, SA), from
  https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/employ
- iptMvMd.xlsx (IPT, industrial production index, SA), from
  https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/ipt

and compares every value Chronicle retains from its four archived releases
(data/archive/) with the RTDSM vintage of the release month. It also checks that
each release's newest month is absent from the previous month's vintage, so the
release month is the first vintage that contains it.

The workbooks are not committed. Only the compared cells, the workbook hashes and
their vintage ranges are written to reports/rtdsm-comparison-2024.json. Run from
the repository root:

    uv run --locked python scripts/rtdsm_comparison.py \\
        --employ employMvMd.xlsx --ipt iptMvMd.xlsx [--check]
"""

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "data" / "archive" / "releases.json"
OUTPUT = ROOT / "reports" / "rtdsm-comparison-2024.json"

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
MISSING = "#N/A"
PAGES = {
    "EMPLOY": "https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/employ",
    "IPT": "https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/ipt",
}


def _column_index(ref: str) -> int:
    index = 0
    for char in re.match(r"[A-Z]+", ref).group():
        index = index * 26 + ord(char) - 64
    return index - 1


def read_first_sheet(path: Path) -> list[list[str | None]]:
    """Return the first worksheet's rows as stored cell text."""
    with zipfile.ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.iter(f"{MAIN}si"):
                shared.append("".join(t.text or "" for t in item.iter(f"{MAIN}t")))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheet = workbook.find(f"{MAIN}sheets/{MAIN}sheet")
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels if r.get("Id") == sheet.get(REL_ID))
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = f"xl/{target}"
        rows = []
        for row in ET.fromstring(archive.read(target)).iter(f"{MAIN}row"):
            cells = {}
            for cell in row.iter(f"{MAIN}c"):
                value = cell.find(f"{MAIN}v")
                kind = cell.get("t")
                if kind == "s" and value is not None:
                    text = shared[int(value.text)]
                elif kind == "inlineStr":
                    text = "".join(t.text or "" for t in cell.iter(f"{MAIN}t"))
                else:
                    text = value.text if value is not None else None
                cells[_column_index(cell.get("r"))] = text
            rows.append([cells.get(i) for i in range(max(cells, default=-1) + 1)])
        return rows


def vintages(rows: list[list[str | None]]) -> dict[str, dict[str, str]]:
    """Map each vintage column to {YYYY-MM-01: value}, leaving out missing cells."""
    header = rows[0]
    if header[0] != "DATE":
        raise ValueError("first column must be DATE")
    result = {name: {} for name in header[1:] if name}
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        year, month = re.fullmatch(r"(\d{4}):(\d{2})", row[0]).groups()
        key = f"{year}-{month}-01"
        for index, name in enumerate(header[1:], start=1):
            if name and index < len(row) and row[index] not in (None, MISSING):
                result[name][key] = row[index]
    return result


def vintage_name(series: str, release_day: str, months_back: int = 0) -> str:
    year, month = int(release_day[:4]), int(release_day[5:7]) - months_back
    while month < 1:
        year, month = year - 1, month + 12
    return f"{series}{year % 100:02d}M{month}"


def retained_releases() -> list[dict]:
    """Read only publisher-produced cells; reference workbooks remain local."""
    records = json.loads(ARCHIVE.read_text(encoding="utf-8"))["releases"]
    return sorted(
        [
            {
                "source_id": row["source_id"],
                "series": "EMPLOY" if row["source_id"].startswith("bls-") else "IPT",
                "release_day": row["release_day"],
                "values": row["values"],
            }
            for row in records
        ],
        key=lambda row: (row["series"], row["release_day"]),
    )


def compare(releases: list[dict], tables: dict[str, dict[str, dict[str, str]]]) -> list[dict]:
    results = []
    for release in releases:
        table = tables[release["series"]]
        current = vintage_name(release["series"], release["release_day"])
        previous = vintage_name(release["series"], release["release_day"], 1)
        reference = max(release["values"])
        cells = []
        for month, value in sorted(release["values"].items()):
            published = table.get(current, {}).get(month)
            cells.append(
                {
                    "month": month,
                    "chronicle": value,
                    "rtdsm": published,
                    "match": published is not None and Decimal(published) == Decimal(value),
                }
            )
        results.append(
            {
                "source_id": release["source_id"],
                "series": release["series"],
                "release_day": release["release_day"],
                "rtdsm_vintage": current,
                "reference_month": reference,
                "previous_vintage": previous,
                "reference_month_in_previous_vintage": reference in table.get(previous, {}),
                "cells": cells,
            }
        )
    return results


def build(employ: Path, ipt: Path) -> dict:
    tables, workbooks = {}, {}
    for series, path in (("EMPLOY", employ), ("IPT", ipt)):
        table = vintages(read_first_sheet(path))
        tables[series] = table
        names = list(table)
        workbooks[series] = {
            "page": PAGES[series],
            "file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "first_vintage": names[0],
            "latest_vintage": names[-1],
        }
    releases = compare(retained_releases(), tables)
    cells = [cell for release in releases for cell in release["cells"]]
    return {
        "schema": "chronicle.rtdsm-comparison.v1",
        "reference": "Federal Reserve Bank of Philadelphia, Real-Time Data Set for "
        "Macroeconomists (RTDSM)",
        "terms": "https://www.philadelphiafed.org/about-us/privacy-notice",
        "rule": "Each retained release is compared with the RTDSM vintage of its release "
        "month. Its newest month must be absent from the previous month's vintage.",
        "workbooks": workbooks,
        "releases": releases,
        "summary": {
            "releases": len(releases),
            "cells": len(cells),
            "matched": sum(cell["match"] for cell in cells),
            "first_included_in_release_month": sum(
                not r["reference_month_in_previous_vintage"] for r in releases
            ),
        },
    }


def render(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--employ", type=Path, required=True, help="employMvMd.xlsx")
    parser.add_argument("--ipt", type=Path, required=True, help="iptMvMd.xlsx")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="compare with the committed report")
    args = parser.parse_args(argv)
    report = build(args.employ, args.ipt)
    text = render(report)
    summary = report["summary"]
    if args.check:
        if args.output.read_text(encoding="utf-8") != text:
            print(f"{args.output} differs from these workbooks", file=sys.stderr)
            return 1
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(
        f"{summary['matched']} of {summary['cells']} cells match; "
        f"{summary['first_included_in_release_month']} of {summary['releases']} releases "
        "first appear in their release-month vintage"
    )
    ok = summary["matched"] == summary["cells"]
    return 0 if ok and summary["first_included_in_release_month"] == summary["releases"] else 1


if __name__ == "__main__":
    sys.exit(main())
