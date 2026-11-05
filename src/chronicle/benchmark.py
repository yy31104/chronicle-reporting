"""Measured synthetic workload in a disposable database; no performance promises."""

import platform
import statistics
import time
from datetime import UTC, date, datetime
from uuid import uuid4

from psycopg import sql

from chronicle import db, store
from chronicle.domain import Occurrence, Query, Revision, SourceAvailability, Unit
from chronicle.reports import build_report, shift_month


def run_benchmark(releases=40, months=120):
    if not 1 <= releases <= 1000 or not 3 <= months <= 1200:
        raise ValueError("Benchmark bounds: releases1..1000, months3..1200")
    name = "chronicle_bench_" + uuid4().hex
    with db.connect("admin") as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER chronicle_owner").format(sql.Identifier(name))
        )
    try:
        with db.connect("admin", database=name) as admin:
            db.migrate(admin)
            server = admin.execute("SHOW server_version").fetchone()[0]
        started = time.perf_counter()
        with db.connect("importer", database=name) as importer:
            store.create_dataset(importer, "benchmark-synthetic")
            rows = tuple(
                Revision(
                    "synthetic",
                    "production",
                    shift_month(date(2000, 1, 1), month),
                    f"release-{release}",
                    release,
                    "documented_sequence",
                    SourceAvailability("date", date(2026, 1, 1)),
                    str(100 + month + release),
                    "value",
                    Unit("index", "v1"),
                    (Occurrence("B1", datetime(2026, 1, 1, tzinfo=UTC)),),
                )
                for release in range(releases)
                for month in range(months)
            )
            store.import_batch(importer, "benchmark-synthetic", "B1", rows)
        import_seconds = time.perf_counter() - started
        with db.connect("publisher", database=name) as publisher:
            store.publish_snapshot(publisher, "benchmark-synthetic", "S1", ["B1"], "b" * 64, None)
        latencies = []
        with db.connect("reader", database=name) as reader:
            context = store.create_context(
                reader,
                "benchmark-synthetic",
                Query(
                    "system_snapshot",
                    "S1",
                    "synthetic",
                    "production",
                    tuple(shift_month(date(2000, 1, 1), m) for m in range(2, months)),
                    expected_unit=Unit("index", "v1"),
                ),
            )
            for _ in range(10):
                started = time.perf_counter()
                result = build_report(reader, context, "production_mean3")
                latencies.append(time.perf_counter() - started)
                assert len(result["rows"]) == months - 2
        return {
            "synthetic": True,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "postgres": server,
            "revision_rows": releases * months,
            "output_months": months - 2,
            "import_seconds": round(import_seconds, 3),
            "report_seconds": latencies,
            "median_seconds": statistics.median(latencies),
            "max_seconds": max(latencies),
            "database_removed": True,
            "network": "local Docker PostgreSQL",
            "runs": 10,
            "scope": "warm single-client full reports; not a public-service capacity claim",
        }
    finally:
        with db.connect("admin") as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
