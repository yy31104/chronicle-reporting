"""Accept the four retained real archives through the private HTTP application.

Run from the repository with PYTHONPATH=src. Reader-role SQL anchors the HTTP
receipts to independently observed original object hashes. Expected cells and
arithmetic are literal, not computed by Chronicle's parser or report helpers.
No raw source fixtures are admitted and no acquisition or migration is performed.
"""

import argparse
import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from chronicle import db

SOURCES = {
    "bls-2024-02-02": {
        "dataset": "macro-archive-bls-v1",
        "snapshot": "BLS-2024-02-02",
        "namespace": "bls.ces.archive",
        "sha256": "bbf1e54126c64258fa0e9dee99812520e6bac52fe5ff3021124e6153b98cd56a",
        "uri": "https://www.bls.gov/news.release/archives/empsit_02022024.htm",
        "cells": {"2023-12-01": "157347", "2024-01-01": "157700"},
    },
    "bls-2024-03-08": {
        "dataset": "macro-archive-bls-v1",
        "snapshot": "BLS-2024-03-08",
        "namespace": "bls.ces.archive",
        "sha256": "7ac4bd1da8ba4eb93d793fd6b6943cc574daf4c24206f90cc8d46172b47c76d1",
        "uri": "https://www.bls.gov/news.release/archives/empsit_03082024.htm",
        "cells": {"2023-12-01": "157304", "2024-01-01": "157533", "2024-02-01": "157808"},
    },
    "frb-2024-02-15": {
        "dataset": "macro-archive-frb-v1",
        "snapshot": "FRB-2024-02-15",
        "namespace": "frb.g17.archive",
        "sha256": "a5e4d43cd1da214c7632def2e328ce50a67d1a9fdd6488de2ca89c3d55f9ac99",
        "uri": "https://www.federalreserve.gov/releases/g17/20240215/",
        "cells": {"2023-11-01": "102.7", "2023-12-01": "102.7", "2024-01-01": "102.6"},
    },
    "frb-2024-03-15": {
        "dataset": "macro-archive-frb-v1",
        "snapshot": "FRB-2024-03-15",
        "namespace": "frb.g17.archive",
        "sha256": "327ea3f28c09bd39ad93b00eb9df494cf771b65a0b3548eaada468da69aee7be",
        "uri": "https://www.federalreserve.gov/releases/g17/20240315/",
        "cells": {
            "2023-11-01": "103.0",
            "2023-12-01": "102.7",
            "2024-01-01": "102.2",
            "2024-02-01": "102.3",
        },
    },
}
UNITS = {
    "bls": {"name": "thousand_persons", "metadata_version": "bls.ces.total_nonfarm_sa.v1"},
    "frb": {"name": "index", "metadata_version": "frb.g17.2017=100.sa.v1"},
}


class AcceptanceError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise AcceptanceError(message)


def digest(value):
    raw = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def captured_sources():
    """Read immutable acquisition receipts; never infer expected numbers from them."""
    captures = {}
    with db.connect("reader") as connection:
        for source_id, expected in SOURCES.items():
            rows = connection.execute(
                "SELECT a.object_sha256,a.source_document FROM chronicle.ingestion_acquisition a "
                "JOIN chronicle.ingestion_request r ON r.request_id=a.request_id "
                "WHERE r.dataset_id=%s AND a.source_id=%s",
                (expected["dataset"], source_id),
            ).fetchall()
            require(len(rows) == 1, f"Expected one retained original acquisition: {source_id}")
            object_hash, document = rows[0]
            manifest = document["manifest"]
            require(
                object_hash == manifest["raw_sha256"] == expected["sha256"],
                "Original source hash mismatch",
            )
            require(
                manifest["synthetic"] is False, "Synthetic source cannot satisfy real acceptance"
            )
            require(manifest["source_uri"] == expected["uri"], "Unexpected publisher URI")
            require(
                {cell["month"]: cell["value"] for cell in manifest["cells"]} == expected["cells"],
                "Captured source cells differ from literal expectation",
            )
            if source_id.startswith("bls"):
                require(
                    manifest["acquisition_channel"] == "operator_supplied_direct_publisher_html",
                    "BLS acquisition provenance changed",
                )
                require(
                    manifest["acquisition"]["authenticity"]
                    == "operator_asserted_not_independently_authenticated",
                    "Browser-saved HTML must not claim independent authentication",
                )
            else:
                require(
                    manifest["acquisition_channel"] == "direct_publisher_archive",
                    "FRB acquisition provenance changed",
                )
            receipts = {
                (occurrence["batch_id"], occurrence["received_at"])
                for revision in document["revisions"]
                for occurrence in revision["occurrences"]
            }
            require(len(receipts) == 1, "Expected one captured acquisition receipt")
            batch, received = receipts.pop()
            stamp = datetime.fromisoformat(received)
            require(
                stamp.tzinfo is not None and stamp.year == 2026,
                "Receipt must be actual acquisition time, not historical release time",
            )
            captures[source_id] = {
                "object_sha256": object_hash,
                "batch_id": batch,
                "received_at": stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "source_uri": expected["uri"],
                "acquisition_channel": manifest["acquisition_channel"],
                "source_release_day": manifest["source_release_day"],
            }
        memberships = {}
        for expected in SOURCES.values():
            key = (expected["dataset"], expected["snapshot"])
            memberships[key] = {
                row[0]
                for row in connection.execute(
                    "SELECT batch_id FROM chronicle.snapshot_batch "
                    "WHERE dataset_id=%s AND snapshot_id=%s",
                    key,
                ).fetchall()
            }
    return captures, memberships


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise AcceptanceError("Redirect refused; bearer remains on the local application")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--evidence", type=Path, default=Path("var/real-source-acceptance.json"))
    args = parser.parse_args()
    require(1024 <= args.port <= 65535, "Invalid private application port")
    require(not args.evidence.exists(), "Evidence path already exists; choose a fresh run path")
    credential_path = (
        Path(db.configuration().get("CHRONICLE_DATA_ROOT", "var")) / "credentials.json"
    )
    try:
        token = json.loads(credential_path.read_text())["viewer"]
        require(isinstance(token, str) and len(token) >= 24, "Invalid local viewer credential")
    except (OSError, ValueError, KeyError):
        raise AcceptanceError("Private runtime credentials unavailable; values omitted") from None
    base = f"http://127.0.0.1:{args.port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(path, payload=None):
        req = urllib.request.Request(
            base + path,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            data=json.dumps(payload).encode() if payload is not None else None,
        )
        try:
            with opener.open(req, timeout=30) as response:
                require(response.status == 200, "Unexpected HTTP response status")
                raw = response.read(16 * 1024 * 1024 + 1)
                require(len(raw) <= 16 * 1024 * 1024, "HTTP evidence exceeds acceptance bound")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise AcceptanceError(
                f"Private API returned HTTP {exc.code}; credentials omitted"
            ) from None

    require(request("/health/ready") == {"status": "ready"}, "Application is not ready")
    captures, memberships = captured_sources()
    documents, checks = {}, []

    def report(
        label,
        source_id,
        value,
        *,
        month="2024-01-01",
        snapshot=None,
        cutoff=None,
        mode="system_snapshot",
    ):
        expected = SOURCES[source_id]
        dataset = expected["dataset"]
        snapshot = snapshot or expected["snapshot"]
        publisher = source_id[:3]
        unit = UNITS[publisher]
        formula = "employment_delta" if publisher == "bls" else "production_mean3"
        series = "employment" if publisher == "bls" else "production"
        months = (
            (
                ["2023-12-01", "2024-01-01"]
                if publisher == "bls"
                else ["2023-11-01", "2023-12-01", "2024-01-01"]
            )
            if month == "2024-01-01"
            else (
                ["2024-01-01", "2024-02-01"]
                if publisher == "bls"
                else ["2023-12-01", "2024-01-01", "2024-02-01"]
            )
        )
        prefix = f"/v1/datasets/{dataset}"
        context = request(
            prefix + "/contexts",
            {
                "context_id": str(uuid4()),
                "snapshot_id": snapshot,
                "mode": mode,
                "source_namespace": expected["namespace"],
                "series_key": series,
                "months": [month],
                "exclusive_end_date": cutoff,
                "expected_unit": unit,
            },
        )
        document = request(
            prefix + "/reports",
            {
                "context_id": context["context_id"],
                "report_id": str(uuid4()),
                "formula": formula,
            },
        )
        require(
            document["dataset_id"] == dataset and document["snapshot_id"] == snapshot,
            "Report scope mismatch",
        )
        require(
            document["mode"] == mode and document["exclusive_end_date"] == cutoff,
            "Report query mode mismatch",
        )
        require(len(document["rows"]) == 1, "Unexpected output grid")
        row = document["rows"][0]
        require(
            row["observation_month"] == month
            and row["status"] == "value"
            and row["value"] == value,
            f"Wrong literal result: {label}",
        )
        require(
            row["unit"] == unit and not row["reasons"] and not row["warnings"],
            "Unexpected result units or status",
        )
        evidence = request(prefix + "/contexts/" + document["input_context_id"] + "/evidence")[
            "inputs"
        ]
        require(
            [item["observation_month"] for item in evidence] == months,
            "Calendar halo is incomplete or reordered",
        )
        require(
            [{k: v for k, v in item.items() if k != "display"} for item in row["inputs"]]
            == evidence,
            "Saved report evidence differs from retained input context",
        )
        for item in evidence:
            selected = item["selected_revision"]
            observation = item["observation_month"]
            for scoped in (item, selected, *selected["occurrences"]):
                require(
                    scoped["dataset_id"] == dataset
                    and scoped["snapshot_id"] == snapshot
                    and scoped["source_namespace"] == expected["namespace"]
                    and scoped["series_key"] == series
                    and scoped["observation_month"] == observation,
                    "Cross-scope evidence detected",
                )
            require(
                item["value"] == selected["lexical_value"] == expected["cells"][observation],
                "Halo value differs from independent source cells",
            )
            require(
                item["status"] == item["source_status"] == selected["source_status"] == "value",
                "Unexpected source status",
            )
            require(
                item["unit"] == selected["unit"] == unit and item["unit_compatible"] is True,
                "Source unit evidence mismatch",
            )
            require(
                item["blocking_revision"] is None and item["availability_unknown"] is False,
                "Unexpected availability uncertainty",
            )
            require(
                selected["source_revision_key"] == source_id
                and selected["availability"] == {"precision": "date", "date": source_id[4:]},
                "Wrong source revision or availability",
            )
            require(
                selected["source_order"] == date.fromisoformat(source_id[4:]).toordinal(),
                "Wrong source order",
            )
            require(len(selected["occurrences"]) == 1, "Unexpected occurrence count")
            occurrence = selected["occurrences"][0]
            capture = captures[source_id]
            require(
                occurrence["source_revision_key"] == source_id
                and occurrence["batch_id"] == capture["batch_id"]
                and occurrence["received_at"] == capture["received_at"],
                "Receipt differs from retained original acquisition",
            )
            require(
                occurrence["batch_id"] in memberships[(dataset, snapshot)],
                "Receipt batch is not a snapshot member",
            )
        require(
            request(prefix + "/reports/" + document["report_id"]) == document,
            "Report replay changed",
        )
        documents[label] = document
        checks.append(
            {
                "case": label,
                "dataset_id": dataset,
                "snapshot_id": snapshot,
                "report_id": document["report_id"],
                "input_context_id": document["input_context_id"],
                "mode": mode,
                "exclusive_end_date": cutoff,
                "output_month": month,
                "value": value,
                "document_sha256": digest(document),
                "replay_equal": True,
                "inputs": evidence,
            }
        )

    report("bls_january_original", "bls-2024-02-02", "353.000000000000")
    report("bls_january_corrected", "bls-2024-03-08", "229.000000000000")
    report("frb_january_original", "frb-2024-02-15", "102.666666666667")
    report("frb_january_corrected", "frb-2024-03-15", "102.633333333333")
    differences = []
    for publisher, expected_difference in (
        ("bls", "-124.000000000000"),
        ("frb", "-0.033333333334"),
    ):
        left, right = (
            documents[publisher + "_january_" + version] for version in ("original", "corrected")
        )
        comparison = request(
            f"/v1/datasets/{left['dataset_id']}/comparisons",
            {"old_report_id": left["report_id"], "new_report_id": right["report_id"]},
        )
        require(
            len(comparison["rows"]) == 1
            and comparison["rows"][0]["status"] == "value"
            and comparison["rows"][0]["value"] == expected_difference,
            "Incorrect stored-output difference",
        )
        differences.append(comparison)
    report(
        "bls_cutoff_excludes_march8",
        "bls-2024-02-02",
        "353.000000000000",
        snapshot="BLS-2024-03-08",
        cutoff="2024-03-08",
        mode="source_as_of",
    )
    report(
        "bls_cutoff_includes_march8",
        "bls-2024-03-08",
        "229.000000000000",
        cutoff="2024-03-09",
        mode="source_as_of",
    )
    report("bls_latest_corrected", "bls-2024-03-08", "229.000000000000", mode="latest_corrected")
    report("frb_latest_corrected", "frb-2024-03-15", "102.633333333333", mode="latest_corrected")
    report("bls_february", "bls-2024-03-08", "275.000000000000", month="2024-02-01")
    report("frb_february", "frb-2024-03-15", "102.400000000000", month="2024-02-01")
    for document in documents.values():
        require(
            request(f"/v1/datasets/{document['dataset_id']}/reports/{document['report_id']}")
            == document,
            "Later report creation changed earlier replay",
        )
    result = {
        "status": "passed",
        "executed_at": datetime.now(UTC).isoformat(),
        "base_url": base,
        "expected_values": "Independent literal table cells and arithmetic, no production helpers",
        "real_sources": captures,
        "reports": checks,
        "comparisons": differences,
        "all_reports_replayed_after_later_requests": True,
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    with args.evidence.open("x") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(
        json.dumps(
            {
                "status": "passed",
                "real_sources": len(captures),
                "reports": len(checks),
                "evidence": str(args.evidence),
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # No request, headers, credential values or driver traceback is logged.
        message = str(error) if isinstance(error, AcceptanceError) else type(error).__name__
        print(json.dumps({"status": "failed", "reason": re.sub(r"[\r\n]", " ", message)[:300]}))
        raise SystemExit(1) from None
