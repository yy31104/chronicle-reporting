"""Small deterministic local CLI; secrets and raw database diagnostics are never printed."""

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from chronicle import db
from chronicle.errors import public_error


def main(argv=None):
    parser = argparse.ArgumentParser(prog="chronicle")
    commands = parser.add_subparsers(dest="command", required=True)
    access = commands.add_parser("access-config")
    access.add_argument("--dataset", action="append", required=True)
    access.add_argument(
        "--directory", type=Path, default=Path(db.configuration().get("CHRONICLE_DATA_ROOT", "var"))
    )
    rotate = commands.add_parser("access-rotate")
    rotate.add_argument(
        "--directory", type=Path, default=Path(db.configuration().get("CHRONICLE_DATA_ROOT", "var"))
    )
    commands.add_parser("demo-synthetic")
    report = commands.add_parser("report")
    report.add_argument("--context", type=UUID, required=True)
    report.add_argument(
        "--formula", choices=["employment_delta", "production_mean3"], required=True
    )
    report.add_argument("--report-id", type=UUID)
    for name in ("replay", "reexecute"):
        command = commands.add_parser(name)
        command.add_argument("report_id", type=UUID)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument(
        "--access-file",
        type=Path,
        default=Path(db.configuration().get("CHRONICLE_ACCESS_FILE", "var/policy/access.json")),
    )
    backup_cmd = commands.add_parser("backup")
    backup_cmd.add_argument("destination", type=Path)
    restore = commands.add_parser("restore-rehearsal")
    restore.add_argument("directory", type=Path)
    restore_cmd = commands.add_parser("restore")
    restore_cmd.add_argument("directory", type=Path)
    restore_cmd.add_argument("--into", required=True)
    restore_cmd.add_argument("--object-root", required=True, type=Path)
    commands.add_parser("verify-objects")
    bench = commands.add_parser("benchmark")
    bench.add_argument("--releases", type=int, default=84)
    bench.add_argument("--months", type=int, default=120)
    ingest = commands.add_parser("import-sources")
    ingest.add_argument("--dataset", required=True)
    ingest.add_argument("--snapshot", required=True)
    ingest.add_argument("--source", action="append", required=True)
    ingest.add_argument(
        "--source-file",
        action="append",
        default=[],
        metavar="SOURCE_ID=PATH",
        help="Import an operator-saved official HTML file with explicit local-file provenance",
    )
    ingest.add_argument("--parent")
    ingest.add_argument("--request-id", type=UUID, required=True)
    repair = commands.add_parser("repair-generation")
    repair.add_argument("--predecessor-dataset", required=True)
    repair.add_argument("--predecessor-snapshot", required=True)
    repair.add_argument("--dataset", required=True)
    repair.add_argument("--snapshot", required=True)
    repair.add_argument("--defect-note", required=True)
    repair.add_argument("--request-id", type=UUID, required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("old_report_id", type=UUID)
    compare.add_argument("new_report_id", type=UUID)
    compare.add_argument(
        "--exact",
        action="store_true",
        help="Also output the exact rational difference rounded once, beside the stored one",
    )
    matrix = commands.add_parser("matrix")
    matrix.add_argument("old_context_id", type=UUID)
    matrix.add_argument("new_context_id", type=UUID)
    matrix.add_argument(
        "--formula", choices=["employment_delta", "production_mean3"], required=True
    )
    matrix.add_argument(
        "--new-code", choices=["fixed-v1", "synthetic-double-v1"], default="fixed-v1"
    )
    matrix.add_argument("--comparison-id", type=UUID, required=True)
    incremental = commands.add_parser("incremental")
    incremental.add_argument("old_report_id", type=UUID)
    incremental.add_argument("new_context_id", type=UUID)
    incremental.add_argument("--report-id", type=UUID, required=True)
    bundle = commands.add_parser("bundle")
    bundle.add_argument("report_id", type=UUID)
    bundle.add_argument("destination", type=Path)
    assistant = commands.add_parser("assistant")
    assistant.add_argument("report_id", type=UUID)
    assistant.add_argument("month")
    args = parser.parse_args(argv)
    try:
        if args.command in {"access-config", "access-rotate"}:
            from chronicle.access import configure_access, rotate_access

            policy, credentials = (
                configure_access(args.directory, args.dataset)
                if args.command == "access-config"
                else rotate_access(args.directory)
            )
            result = {
                "policy_file": str(policy.resolve()),
                "credentials_file": str(credentials.resolve()),
            }
        elif args.command == "demo-synthetic":
            from chronicle.demo import run

            result = run()
        elif args.command == "backup":
            from chronicle.ops import backup

            result = backup(args.destination)
        elif args.command == "restore-rehearsal":
            from chronicle.ops import restore_rehearsal

            result = restore_rehearsal(args.directory)
        elif args.command == "restore":
            from chronicle.ops import restore

            result = restore(args.directory, into=args.into, object_root=args.object_root)
        elif args.command == "benchmark":
            from chronicle.benchmark import run_benchmark

            result = run_benchmark(args.releases, args.months)
        elif args.command in {"import-sources", "repair-generation"}:
            from chronicle.ingestion import import_sources, repair_generation

            with db.connect("importer") as importer, db.connect("publisher") as publisher:
                if args.command == "import-sources":
                    from chronicle.errors import ContractError

                    local_files = {}
                    for item in args.source_file:
                        source_id, separator, path = item.partition("=")
                        if not separator or not path or source_id in local_files:
                            raise ContractError("INVALID_IDENTITY", "Invalid saved source mapping")
                        local_files[source_id] = Path(path)
                    snapshot = import_sources(
                        importer,
                        publisher,
                        args.dataset,
                        args.snapshot,
                        tuple(args.source),
                        args.parent,
                        args.request_id,
                        local_files=local_files or None,
                    )
                else:
                    snapshot = repair_generation(
                        importer,
                        publisher,
                        args.predecessor_dataset,
                        args.predecessor_snapshot,
                        args.dataset,
                        args.snapshot,
                        args.defect_note,
                        args.request_id,
                    )
                result = {
                    "dataset_id": args.dataset,
                    "snapshot_id": snapshot,
                    "request_id": str(args.request_id),
                }
        elif args.command == "serve":
            import uvicorn

            from chronicle.api import create_app

            uvicorn.run(
                create_app(access_path=args.access_file),
                host="127.0.0.1",
                port=args.port,
                access_log=False,
                limit_concurrency=32,
                timeout_keep_alive=5,
                timeout_graceful_shutdown=30,
                proxy_headers=False,
            )
            return 0
        else:
            from chronicle.reports import build_report, reexecute_report, replay_report

            with db.connect("reader") as conn:
                from chronicle.objects import require_snapshot_access

                def check_context(context_id):
                    row = conn.execute(
                        "SELECT dataset_id,snapshot_id FROM chronicle.analysis_context "
                        "WHERE context_id=%s",
                        (context_id,),
                    ).fetchone()
                    if row:
                        require_snapshot_access(conn, *row)

                def check_report(report_id):
                    document = replay_report(conn, report_id)
                    require_snapshot_access(conn, document["dataset_id"], document["snapshot_id"])
                    return document

                if args.command == "verify-objects":
                    from chronicle.objects import verify_closure

                    failures = verify_closure(conn)
                    result = {"status": "blocked" if failures else "verified", "failures": failures}
                elif args.command == "compare":
                    from chronicle.comparison import compare_reports

                    check_report(args.old_report_id)
                    check_report(args.new_report_id)
                    if args.exact:
                        from chronicle.exact_revision import compare_reports_exact

                        result = compare_reports_exact(conn, args.old_report_id, args.new_report_id)
                    else:
                        result = compare_reports(conn, args.old_report_id, args.new_report_id)
                elif args.command == "matrix":
                    from chronicle.comparison import compare_matrix

                    check_context(args.old_context_id)
                    check_context(args.new_context_id)
                    result = compare_matrix(
                        conn,
                        args.old_context_id,
                        args.new_context_id,
                        args.formula,
                        comparison_id=args.comparison_id,
                        new_code=args.new_code,
                    )
                elif args.command == "incremental":
                    from chronicle.incremental import build_incremental_report

                    check_report(args.old_report_id)
                    check_context(args.new_context_id)
                    result = build_incremental_report(
                        conn, args.old_report_id, args.new_context_id, report_id=args.report_id
                    )
                elif args.command == "bundle":
                    from chronicle.bundles import export_report_bundle, verify_bundle

                    payload = export_report_bundle(conn, args.report_id)
                    with args.destination.open("xb") as output:
                        output.write(payload)
                    result = {
                        "file": str(args.destination.resolve()),
                        "manifest": verify_bundle(payload)["manifest"],
                    }
                elif args.command == "assistant":
                    from chronicle.assistant import answer_report

                    result = answer_report(check_report(args.report_id), args.month)
                elif args.command == "report":
                    check_context(args.context)
                    result = build_report(
                        conn, args.context, args.formula, report_id=args.report_id
                    )
                elif args.command == "reexecute":
                    check_report(args.report_id)
                    result = reexecute_report(conn, args.report_id)
                else:
                    result = check_report(args.report_id)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 1 if isinstance(result, dict) and result.get("status") == "blocked" else 0
    except Exception as exc:
        print(json.dumps(public_error(exc), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
