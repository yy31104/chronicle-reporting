"""Install and execute the exported environment, not merely its checksums."""

import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest

from chronicle import bundles, reports, store
from chronicle.domain import Query
from tests.postgres.conftest import import_fixture


def test_bundle_can_install_locked_offline_and_reexecute_retained_inputs(db, tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the exported-environment installation check")
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/golden/G1.json").read_text())
    import_fixture(db, fixture)
    context = store.create_context(
        db.reader,
        db.dataset_id,
        Query("system_snapshot", "S1", "synthetic", "employment", (date(2026, 1, 1),)),
    )
    report = reports.build_report(db.reader, context, "employment_delta")
    archive = bundles.export_report_bundle(db.reader, UUID(report["report_id"]))
    checked = bundles.verify_bundle(archive)
    assert checked["manifest"]["environment_available"]
    assert checked["manifest"]["dependency_wheels"] == "included", checked["manifest"][
        "dependency_wheels_limitation"
    ]
    # Only this locally produced and validated archive is extracted for rehearsal.
    with zipfile.ZipFile(io.BytesIO(archive)) as handle:
        handle.extractall(tmp_path)
    environment = tmp_path / "environment"
    child_env = dict(os.environ)
    child_env.pop("PYTHONPATH", None)
    child_env.pop("VIRTUAL_ENV", None)
    # An empty cache: everything must come from the bundle's own wheels.
    child_env["UV_CACHE_DIR"] = str(tmp_path / "empty-uv-cache")
    for command in (
        [uv, "venv", "--python", sys.executable, ".venv"],
        [
            uv,
            "pip",
            "install",
            "--python",
            ".venv/bin/python",
            "--offline",
            "--no-index",
            "--find-links",
            "wheels",
            "--require-hashes",
            "-r",
            "src/chronicle/runtime-requirements.lock",
        ],
    ):
        installed = subprocess.run(
            command,
            cwd=environment,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert installed.returncode == 0, installed.stderr
    child_env["PYTHONPATH"] = str(environment / "src")
    script = """
import json,sys
from datetime import date
from importlib.resources import files
from chronicle.reports import implementation_identity,compose_rows
report=json.load(sys.stdin)
inputs={i["observation_month"]:{k:v for k,v in i.items() if k!="display"}
        for r in report["rows"] for i in r["inputs"]}
months=tuple(date.fromisoformat(m) for m in report["output_months"])
actual=compose_rows(report["formula"],months,list(inputs.values()))
print(json.dumps({"identity_matches":implementation_identity()==report["implementation"],
"rows_equal":actual==report["rows"],
"sql_present":files("chronicle").joinpath("sql/select_evidence_v1.sql").is_file(),
"static_present":files("chronicle").joinpath("static/app.js").is_file()}))
"""
    executed = subprocess.run(
        [str(environment / ".venv/bin/python"), "-c", script],
        cwd=environment,
        env=child_env,
        input=json.dumps(report),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert executed.returncode == 0, executed.stderr
    assert json.loads(executed.stdout) == {
        "identity_matches": True,
        "rows_equal": True,
        "sql_present": True,
        "static_present": True,
    }
