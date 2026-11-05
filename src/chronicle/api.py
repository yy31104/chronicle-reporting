"""Authenticated, bounded HTTP delivery of pinned reports; no user SQL or fetch URLs."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from chronicle import db, store
from chronicle.access import Principal, authenticate, load_policy
from chronicle.domain import Query, SourceCutoff, Unit
from chronicle.errors import ContractError, public_error
from chronicle.evidence import select_evidence
from chronicle.jobs import JobManager
from chronicle.reports import build_report, replay_report

LOG = logging.getLogger("chronicle.http")
MAX_BODY = 65536


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UnitInput(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    metadata_version: str = Field(min_length=1, max_length=200)


class ContextInput(StrictModel):
    context_id: UUID
    snapshot_id: str = Field(min_length=1, max_length=200)
    mode: Literal["latest_corrected", "source_as_of", "system_snapshot"]
    source_namespace: str = Field(min_length=1, max_length=200)
    series_key: str = Field(min_length=1, max_length=200)
    months: list[str] = Field(min_length=1, max_length=120)
    exclusive_end_date: str | None = None
    expected_unit: UnitInput | None = None

    def query(self) -> Query:
        try:
            months = tuple(date.fromisoformat(m) for m in self.months)
            if any(m.isoformat() != raw for m, raw in zip(months, self.months, strict=True)):
                raise ValueError
            cutoff = (
                date.fromisoformat(self.exclusive_end_date) if self.exclusive_end_date else None
            )
            if cutoff and cutoff.isoformat() != self.exclusive_end_date:
                raise ValueError
        except ValueError:
            raise ContractError("INVALID_DATE", "Dates must use YYYY-MM-DD") from None
        return Query(
            self.mode,
            self.snapshot_id,
            self.source_namespace,
            self.series_key,
            months,
            SourceCutoff(cutoff) if cutoff else None,
            Unit(**self.expected_unit.model_dump()) if self.expected_unit else None,
        )


class ReportInput(StrictModel):
    context_id: UUID
    report_id: UUID
    formula: Literal["employment_delta", "production_mean3"]


class ComparisonInput(StrictModel):
    old_report_id: UUID
    new_report_id: UUID


class MatrixInput(StrictModel):
    old_context_id: UUID
    new_context_id: UUID
    comparison_id: UUID
    formula: Literal["employment_delta", "production_mean3"]
    old_code: Literal["fixed-v1", "synthetic-double-v1"] = "fixed-v1"
    new_code: Literal["fixed-v1", "synthetic-double-v1"] = "fixed-v1"


class BodyLimit:
    """Cap streamed bodies before JSON parsing, even without Content-Length."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope.get("headers", [])).get(b"content-length", b"0")
        try:
            oversized = int(length) > MAX_BODY or int(length) < 0
        except ValueError:
            oversized = True
        if oversized:
            return await JSONResponse({"code": "REQUEST_TOO_LARGE"}, 413)(scope, receive, send)
        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > MAX_BODY:
                raise ContractError("REQUEST_TOO_LARGE", "Request exceeds limit")
            return message

        await self.app(scope, limited_receive, send)


def create_app(*, access_path: Path | None = None, connect=None) -> FastAPI:
    access_path = access_path or Path(
        db.configuration().get("CHRONICLE_ACCESS_FILE", "var/policy/access.json")
    )
    connection = connect or db.connect
    capacity = asyncio.Semaphore(2)
    readiness_capacity = asyncio.Semaphore(1)
    jobs = JobManager(connection, workers=2)
    metrics = {"requests": 0, "errors": 0, "elapsed_ms": 0}
    if not LOG.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False

    @asynccontextmanager
    async def lifespan(app):
        # No anonymous mode, default secret or migration with runtime credentials.
        if not access_path.is_file():
            raise RuntimeError("Access policy missing; run chronicle access-config")
        load_policy(access_path)
        try:
            yield
        finally:
            await run_in_threadpool(jobs.close)

    app = FastAPI(
        title="Chronicle",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(BodyLimit)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    async def principal(authorization: Annotated[str | None, Header()] = None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise ContractError("UNAUTHENTICATED", "Authentication required")
        try:
            return authenticate(authorization[7:], access_path)
        except (OSError, ValueError, KeyError, TypeError):
            raise ContractError("UNAUTHENTICATED", "Authentication required") from None

    identity = Annotated[Principal, Depends(principal)]

    @app.middleware("http")
    async def headers_and_audit(request: Request, call_next):
        started = time.monotonic()
        request_id = str(uuid4())
        try:
            response = await call_next(request)
        except Exception as exc:
            response = error_response(exc)
        response.headers.update(
            {
                "X-Request-ID": request_id,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'self'; frame-ancestors 'none'; object-src 'none'"
                ),
            }
        )
        elapsed = int((time.monotonic() - started) * 1000)
        metrics["requests"] += 1
        metrics["errors"] += int(response.status_code >= 400)
        metrics["elapsed_ms"] += elapsed
        LOG.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "route": getattr(request.scope.get("route"), "path", "unmatched"),
                    "status": response.status_code,
                    "elapsed_ms": elapsed,
                }
            )
        )
        return response

    def error_response(exc):
        error = public_error(exc)
        code = error["code"]
        status = (
            401
            if code == "UNAUTHENTICATED"
            else 403
            if code in {"ACCESS_DENIED", "RIGHTS_DENIED", "RIGHTS_REVOKED"}
            else 413
            if code
            in {"REQUEST_TOO_LARGE", "EVIDENCE_TOO_LARGE", "REPORT_TOO_LARGE", "BUNDLE_TOO_LARGE"}
            else 404
            if code.endswith("NOT_FOUND") or code in {"UNKNOWN_SNAPSHOT", "UNKNOWN_DATASET"}
            else 503
            if error["retryable"] or code == "SERVICE_BUSY" or not isinstance(exc, ContractError)
            else 409
            if code.endswith("CONFLICT") or code == "SNAPSHOT_BASE_CHANGED"
            else 503
            if code == "SNAPSHOT_EVIDENCE_UNAVAILABLE"
            else 422
        )
        return JSONResponse(
            error, status, headers={"WWW-Authenticate": "Bearer"} if status == 401 else None
        )

    @app.exception_handler(ContractError)
    async def contract_error(request, exc):
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse(
            {"code": "INVALID_REQUEST", "message": "Request schema is invalid", "retryable": False},
            422,
        )

    async def execute(action):
        # Bounded admission; reject excess work rather than accumulating a queue.
        if capacity.locked():
            return JSONResponse({"code": "SERVICE_BUSY", "retryable": True}, 503)
        async with capacity:
            return await run_in_threadpool(action)

    def context_scope(conn, context_id, dataset_id):
        found = conn.execute(
            "SELECT dataset_id,snapshot_id,cardinality(months) FROM chronicle.analysis_context "
            "WHERE context_id=%s",
            (context_id,),
        ).fetchone()
        if not found or found[0] != dataset_id:
            raise ContractError("CONTEXT_NOT_FOUND", "Context not found")
        if found[2] > 120:
            raise ContractError("REQUEST_TOO_LARGE", "HTTP contexts allow at most120 months")
        snapshot_access(conn, dataset_id, found[1])

    def snapshot_access(conn, dataset_id, snapshot_id):
        from chronicle.objects import require_snapshot_access

        require_snapshot_access(conn, dataset_id, snapshot_id)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        def probe():
            load_policy(access_path)
            with connection("reader") as conn:
                conn.execute("SELECT 1 FROM chronicle.report_run LIMIT 0")
                conn.execute("SELECT 1 FROM chronicle.comparison_run LIMIT 0")
                conn.execute("SELECT 1 FROM chronicle.analysis_context LIMIT 0")
            return {"status": "ready"}

        # Readiness is bounded independently of report admission. A busy but
        # functioning service must not be marked unhealthy by its own work queue.
        if readiness_capacity.locked():
            return JSONResponse({"code": "READINESS_BUSY", "retryable": True}, 503)
        async with readiness_capacity:
            return await run_in_threadpool(probe)

    @app.get("/v1/datasets")
    async def datasets(user: identity):
        return {"datasets": sorted(user.datasets), "role": user.role}

    @app.post("/v1/datasets/{dataset_id}/contexts")
    async def context(dataset_id: str, payload: ContextInput, user: identity):
        user.require(dataset_id)
        query = payload.query()

        def action():
            with connection("reader") as conn:
                snapshot_access(conn, dataset_id, query.snapshot_id)
                return {
                    "context_id": str(
                        store.create_context(conn, dataset_id, query, payload.context_id)
                    )
                }

        return await execute(action)

    @app.get("/v1/datasets/{dataset_id}/contexts/{context_id}/evidence")
    async def evidence(dataset_id: str, context_id: UUID, user: identity):
        user.require(dataset_id)

        def action():
            with connection("reader") as conn:
                context_scope(conn, context_id, dataset_id)
                return {"inputs": select_evidence(conn, context_id)}

        return await execute(action)

    @app.post("/v1/datasets/{dataset_id}/reports")
    async def report(dataset_id: str, payload: ReportInput, user: identity):
        user.require(dataset_id)

        def action():
            with connection("reader") as conn:
                context_scope(conn, payload.context_id, dataset_id)
                return build_report(
                    conn, payload.context_id, payload.formula, report_id=payload.report_id
                )

        return await execute(action)

    @app.get("/v1/datasets/{dataset_id}/reports/{report_id}")
    async def replay(dataset_id: str, report_id: UUID, user: identity):
        user.require(dataset_id)

        def action():
            with connection("reader") as conn:
                result = replay_report(conn, report_id)
                if result["dataset_id"] != dataset_id:
                    raise ContractError("REPORT_NOT_FOUND", "Report not found")
                snapshot_access(conn, dataset_id, result["snapshot_id"])
                return result

        return await execute(action)

    @app.get("/v1/datasets/{dataset_id}/series")
    async def series(dataset_id: str, user: identity):
        user.require(dataset_id)

        def action():
            with connection("reader") as conn:
                rows = conn.execute(
                    "SELECT source_namespace,series_key FROM chronicle.series "
                    'WHERE dataset_id=%s ORDER BY source_namespace COLLATE "C",'
                    'series_key COLLATE "C" LIMIT 101',
                    (dataset_id,),
                ).fetchall()
                return {
                    "series": [
                        {"source_namespace": namespace, "series_key": key}
                        for namespace, key in rows[:100]
                    ],
                    "limit": 100,
                    "truncated": len(rows) > 100,
                }

        return await execute(action)

    @app.get("/v1/datasets/{dataset_id}/snapshots")
    async def snapshots(dataset_id: str, user: identity):
        user.require(dataset_id)

        def action():
            with connection("reader") as conn:
                return {
                    "snapshots": [
                        row[0]
                        for row in conn.execute(
                            "SELECT snapshot_id FROM chronicle.snapshot WHERE dataset_id=%s "
                            "ORDER BY created_at DESC,snapshot_id LIMIT 100",
                            (dataset_id,),
                        )
                    ]
                }

        return await execute(action)

    @app.post("/v1/datasets/{dataset_id}/jobs", status_code=202)
    async def start_job(dataset_id: str, payload: ReportInput, user: identity):
        user.require(dataset_id)

        def action(conn):
            context_scope(conn, payload.context_id, dataset_id)
            return build_report(
                conn, payload.context_id, payload.formula, report_id=payload.report_id
            )

        @contextmanager
        def guard():
            from chronicle.objects import snapshot_pin

            with connection("reader") as pins:
                record = pins.execute(
                    "SELECT dataset_id,snapshot_id FROM chronicle.analysis_context "
                    "WHERE context_id=%s",
                    (payload.context_id,),
                ).fetchone()
                if not record or record[0] != dataset_id:
                    raise ContractError("CONTEXT_NOT_FOUND", "Context not found")
                with snapshot_pin(pins, *record):
                    yield

        return jobs.submit(dataset_id, user.name, payload.report_id, action, guard=guard).snapshot()

    @app.get("/v1/datasets/{dataset_id}/jobs/{job_id}")
    async def job_status(dataset_id: str, job_id: UUID, user: identity):
        user.require(dataset_id)
        job = jobs.get(str(job_id), dataset_id, user.name)
        result = job.snapshot()
        if result["result"]:

            def check():
                with connection("reader") as conn:
                    result["result"] = authorized_report(conn, dataset_id, UUID(job.report_id))
                return result

            return await execute(check)
        return result

    @app.delete("/v1/datasets/{dataset_id}/jobs/{job_id}", status_code=202)
    async def cancel_job(dataset_id: str, job_id: UUID, user: identity):
        user.require(dataset_id)
        job = jobs.get(str(job_id), dataset_id, user.name)
        await run_in_threadpool(jobs.cancel, job)
        result = job.snapshot()
        result.pop("result")
        return result

    @app.get("/v1/datasets/{dataset_id}/reports/{report_id}/assistant/{month}")
    async def assistant(dataset_id: str, report_id: UUID, month: str, user: identity):
        user.require(dataset_id)

        def action():
            from chronicle.assistant import answer_report

            with connection("reader") as conn:
                document = replay_report(conn, report_id)
                if document["dataset_id"] != dataset_id:
                    raise ContractError("REPORT_NOT_FOUND", "Report not found")
                snapshot_access(conn, dataset_id, document["snapshot_id"])
                return answer_report(document, month)

        return await execute(action)

    def authorized_report(conn, dataset_id, report_id, use="local_analysis"):
        from chronicle.objects import require_snapshot_access

        document = replay_report(conn, report_id)
        if document["dataset_id"] != dataset_id:
            raise ContractError("REPORT_NOT_FOUND", "Report not found")
        require_snapshot_access(conn, dataset_id, document["snapshot_id"], use)
        return document

    @app.post("/v1/datasets/{dataset_id}/comparisons")
    async def compare(dataset_id: str, payload: ComparisonInput, user: identity):
        user.require(dataset_id)

        def action():
            from chronicle.comparison import compare_reports

            with connection("reader") as conn:
                authorized_report(conn, dataset_id, payload.old_report_id)
                authorized_report(conn, dataset_id, payload.new_report_id)
                return compare_reports(conn, payload.old_report_id, payload.new_report_id)

        return await execute(action)

    @app.post("/v1/datasets/{dataset_id}/exact-comparisons")
    async def exact_compare(dataset_id: str, payload: ComparisonInput, user: identity):
        """Stored-output difference unchanged, plus the exact difference rounded once."""
        user.require(dataset_id)

        def action():
            from chronicle.exact_revision import compare_reports_exact

            with connection("reader") as conn:
                authorized_report(conn, dataset_id, payload.old_report_id)
                authorized_report(conn, dataset_id, payload.new_report_id)
                return compare_reports_exact(conn, payload.old_report_id, payload.new_report_id)

        return await execute(action)

    @app.post("/v1/datasets/{dataset_id}/comparison-export")
    async def comparison_export(dataset_id: str, payload: ComparisonInput, user: identity):
        user.require(dataset_id)

        def action():
            with connection("reader") as conn:
                left = authorized_report(conn, dataset_id, payload.old_report_id, "public_export")
                right = authorized_report(conn, dataset_id, payload.new_report_id, "public_export")
                return JSONResponse(
                    {"left": left, "right": right},
                    headers={
                        "Content-Disposition": 'attachment; filename="chronicle-comparison.json"'
                    },
                )

        return await execute(action)

    @app.post("/v1/datasets/{dataset_id}/matrix")
    async def matrix(dataset_id: str, payload: MatrixInput, user: identity):
        user.require(dataset_id)

        def action():
            from chronicle.comparison import compare_matrix

            with connection("reader") as conn:
                context_scope(conn, payload.old_context_id, dataset_id)
                context_scope(conn, payload.new_context_id, dataset_id)
                return compare_matrix(
                    conn,
                    payload.old_context_id,
                    payload.new_context_id,
                    payload.formula,
                    comparison_id=payload.comparison_id,
                    old_code=payload.old_code,
                    new_code=payload.new_code,
                )

        return await execute(action)

    @app.get("/v1/datasets/{dataset_id}/operations")
    async def operations(dataset_id: str, user: identity):
        user.require(dataset_id, operator=True)

        def action():
            with connection("reader") as conn:
                batches = conn.execute(
                    "SELECT state,count(*) FROM chronicle.import_batch "
                    "WHERE dataset_id=%s GROUP BY state",
                    (dataset_id,),
                ).fetchall()
                active = conn.execute(
                    "SELECT count(*) FROM chronicle.staging_ticket "
                    "WHERE dataset_id=%s AND state='active'",
                    (dataset_id,),
                ).fetchone()[0]
                return {
                    "batches": dict(batches),
                    "active_staging_tickets": active,
                    "published_gc": "disabled",
                    "job_capacity": 2,
                    "http_metrics": dict(metrics),
                }

        return await execute(action)

    @app.get("/")
    async def index():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/assets/{name}")
    async def asset(name: Literal["app.js", "style.css"]):
        return FileResponse(Path(__file__).parent / "static" / name)

    return app
