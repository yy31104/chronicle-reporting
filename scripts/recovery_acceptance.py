"""Restore a trusted backup, switch an isolated app to it, and verify real reports.

Run from the repository with PYTHONPATH=src. Secrets stay outside the checkout and
are never included in the emitted evidence. The primary app/database are untouched.
"""

import argparse
import json
import os
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from chronicle import db
from chronicle.access import configure_access
from chronicle.ops import restore
from chronicle.reports import replay_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("--database", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--require-four-sources", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    args.root.mkdir(mode=0o700)  # Must be new; no credentials overwritten.
    recovered = restore(args.backup, into=args.database, object_root=args.root / "objects")
    with db.connect("reader", database=args.database) as reader:
        documents = [
            replay_report(reader, row[0])
            for row in reader.execute(
                "SELECT report_id FROM chronicle.report_run "
                "WHERE document->>'source_namespace' IN ('bls.ces.archive','frb.g17.archive') "
                "ORDER BY created_at,report_id"
            ).fetchall()
        ]
    # One report for each actual real snapshot; no synthetic substitute is accepted.
    selected = {}
    for document in documents:
        selected.setdefault((document["dataset_id"], document["snapshot_id"]), document)
    if len(selected) < 2:
        raise RuntimeError("No retained real source reports to accept")
    if args.require_four_sources:
        required = {
            ("macro-archive-bls-v1", "BLS-2024-02-02"),
            ("macro-archive-bls-v1", "BLS-2024-03-08"),
            ("macro-archive-frb-v1", "FRB-2024-02-15"),
            ("macro-archive-frb-v1", "FRB-2024-03-15"),
        }
        if not required.issubset(selected):
            raise RuntimeError("Four-source recovery requires all four authentic snapshots")
    _, credential_file = configure_access(args.root, sorted({key[0] for key in selected}))
    token = json.loads(credential_file.read_text())["viewer"]
    environment = {
        **os.environ,
        **db.configuration(),
        "CHRONICLE_APP_UID": str(os.getuid()),
        "CHRONICLE_APP_GID": str(os.getgid()),
        "CHRONICLE_RECOVERY_ROOT": str(args.root),
        "CHRONICLE_RECOVERY_DATABASE": args.database,
        "CHRONICLE_RECOVERY_PORT": str(args.port),
    }
    project = "chronicle-recovery-" + uuid4().hex[:10]
    compose = ["docker", "compose", "-p", project, "-f", "compose.recovery.yaml"]
    origin = f"http://127.0.0.1:{args.port}"

    def command(*arguments):
        subprocess.run(
            [*compose, *arguments],
            env=environment,
            check=True,
            timeout=120,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def request(path, payload=None):
        req = urllib.request.Request(
            origin + path,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            data=json.dumps(payload).encode() if payload is not None else None,
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            assert response.status == 200
            return json.load(response)

    results = []
    try:
        command("up", "-d", "--wait")
        assert request("/health/ready") == {"status": "ready"}
        for (dataset, snapshot), document in selected.items():
            base = f"/v1/datasets/{dataset}"
            replay = request(base + "/reports/" + document["report_id"])
            assert replay == document
            evidence = request(base + "/contexts/" + document["input_context_id"] + "/evidence")
            assert evidence["inputs"] and all(
                row["dataset_id"] == dataset and row["snapshot_id"] == snapshot
                for row in evidence["inputs"]
            )
            context = request(
                base + "/contexts",
                {
                    "context_id": str(uuid4()),
                    "snapshot_id": snapshot,
                    "mode": document["mode"],
                    "source_namespace": document["source_namespace"],
                    "series_key": document["series_key"],
                    "months": document["output_months"],
                    "exclusive_end_date": document["exclusive_end_date"],
                    "expected_unit": document["expected_unit"],
                },
            )
            fresh = request(
                base + "/reports",
                {
                    "context_id": context["context_id"],
                    "report_id": str(uuid4()),
                    "formula": document["formula"],
                },
            )
            assert fresh["rows"] == document["rows"]
            results.append(
                {
                    "dataset": dataset,
                    "snapshot": snapshot,
                    "report_id": document["report_id"],
                    "new_report_id": fresh["report_id"],
                    "replay_equal": True,
                    "fresh_rows_equal": True,
                    "evidence_slots": len(evidence["inputs"]),
                }
            )
        # Restart exercises continued use of the restored DB and mounted objects.
        command("restart")
        for _ in range(30):
            try:
                request("/health/ready")
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("Recovered application did not become ready after restart")
        for result in results:
            request(f"/v1/datasets/{result['dataset']}/reports/{result['new_report_id']}")
        args.evidence.write_text(
            json.dumps(
                {
                    "executed_at": datetime.now(UTC).isoformat(),
                    "restore": recovered,
                    "project": project,
                    "url": origin,
                    "reports": results,
                    "restart_replay": "passed",
                    "primary_configuration_changed": False,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        command("down")
    print(
        json.dumps(
            {"status": "verified", "real_snapshots": len(results), "evidence": str(args.evidence)}
        )
    )


if __name__ == "__main__":
    main()
