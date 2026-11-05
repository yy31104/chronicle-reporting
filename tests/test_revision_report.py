"""The committed revision statistics report must regenerate byte-for-byte."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_script():
    path = ROOT / "scripts" / "revision_statistics_report.py"
    spec = importlib.util.spec_from_file_location("revision_statistics_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_report_is_current():
    script = load_script()
    committed = (ROOT / "reports" / "revision-statistics-2024-01.json").read_text("utf-8")
    assert script.render() == committed


def test_headline_january_payroll_example_is_353_to_229():
    report = json.loads((ROOT / "reports" / "revision-statistics-2024-01.json").read_text("utf-8"))
    change = next(
        s for s in report["series"] if s["series"] == "bls.ces.total_nonfarm.sa.monthly_change"
    )
    (january,) = change["revisions"]
    assert january["first_release_value"] == "353.000000000000"
    assert january["later"][0]["value"] == "229.000000000000"
    assert january["later"][0]["revision"] == "-124.000000000000"
    assert change["statistics"][-1]["direction"] == {"up": 0, "down": 1, "unchanged": 0}
