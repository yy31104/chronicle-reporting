"""RTDSM comparison: workbook parsing, vintage matching and the committed report."""

import importlib.util
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_script():
    path = ROOT / "scripts" / "rtdsm_comparison.py"
    spec = importlib.util.spec_from_file_location("rtdsm_comparison", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_workbook(path, rows):
    """Write a minimal xlsx whose text cells use the shared-string table."""
    strings, sheet_rows = [], []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if re.fullmatch(r"-?\d+(\.\d+)?", value):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                strings.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
            '<sheet name="employ" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            f'<sst xmlns="{main}">' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{main}"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>',
        )


def test_workbook_columns_become_vintages_without_missing_cells(tmp_path):
    script = load_script()
    path = tmp_path / "employ.xlsx"
    write_workbook(
        path,
        [
            ["DATE", "EMPLOY24M1", "EMPLOY24M2"],
            ["2023:12", "900.0000", "1000.0000"],
            ["2024:01", "#N/A", "1100.0000"],
        ],
    )
    assert script.vintages(script.read_first_sheet(path)) == {
        "EMPLOY24M1": {"2023-12-01": "900.0000"},
        "EMPLOY24M2": {"2023-12-01": "1000.0000", "2024-01-01": "1100.0000"},
    }


def test_vintage_name_uses_release_month_and_rolls_over_the_year():
    script = load_script()
    assert script.vintage_name("EMPLOY", "2024-02-02") == "EMPLOY24M2"
    assert script.vintage_name("IPT", "2024-03-15", 1) == "IPT24M2"
    assert script.vintage_name("EMPLOY", "2024-01-05", 1) == "EMPLOY23M12"


def test_compare_flags_wrong_missing_and_previously_published_values():
    script = load_script()
    release = {
        "source_id": "bls-x",
        "series": "EMPLOY",
        "release_day": "2024-03-08",
        "values": {"2023-12-01": "1001", "2024-01-01": "1110", "2024-02-01": "1220"},
    }
    tables = {
        "EMPLOY": {
            "EMPLOY24M2": {"2024-01-01": "1100.0000", "2024-02-01": "1200.0000"},
            "EMPLOY24M3": {"2023-12-01": "1001.0000", "2024-01-01": "1111.0000"},
        }
    }
    (result,) = script.compare([release], tables)
    assert result["rtdsm_vintage"] == "EMPLOY24M3"
    assert [(c["rtdsm"], c["match"]) for c in result["cells"]] == [
        ("1001.0000", True),
        ("1111.0000", False),
        (None, False),
    ]
    assert result["reference_month_in_previous_vintage"] is True


def test_retained_releases_contain_only_the_twelve_publisher_cells():
    releases = load_script().retained_releases()
    assert len(releases) == 4
    assert sum(len(release["values"]) for release in releases) == 12
    assert next(r for r in releases if r["source_id"] == "bls-2024-03-08")["values"] == {
        "2023-12-01": "157304",
        "2024-01-01": "157533",
        "2024-02-01": "157808",
    }
    assert not (ROOT / "reports" / "rtdsm-comparison-2024.json").exists()
