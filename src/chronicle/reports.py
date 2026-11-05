"""Exact fixed monthly reports over immutable PostgreSQL contexts and evidence.

Identity replay returns retained bytes (after digest verification). Reexecution
requires the recorded implementation/runtime/lock identity and recomputes against
the retained input context; it never resolves the current dataset pointer.
"""

import hashlib
import json
import platform
from datetime import date
from importlib import metadata
from importlib.resources import files
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from chronicle.domain import Query, SourceCutoff, Unit
from chronicle.errors import ContractError
from chronicle.evidence import select_evidence
from chronicle.numeric import SCALE, canonical_ratio, format_scaled, parse_scaled
from chronicle.store import create_context

REPORT_SCHEMA = "chronicle.monthly-report.v1"
NUMERIC_POLICY = "decimal12-half-even-v1"
FORMULAS = {"employment_delta": 1, "production_mean3": 2}
MAX_OUTPUT_MONTHS = 2400
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def implementation_identity() -> dict:
    """Content identity works in both a source checkout and an installed wheel."""
    package = files("chronicle")
    names = [
        "reports.py",
        "evidence.py",
        "numeric.py",
        "domain.py",
        "store.py",
        "errors.py",
        "incremental.py",
        "sql/select_evidence_v1.sql",
        "sql/select_context.sql",
    ]
    sources = {
        name: hashlib.sha256(package.joinpath(name).read_bytes()).hexdigest() for name in names
    }
    lock_hash = package.joinpath("runtime-lock.sha256").read_text().strip()
    if len(lock_hash) != 64 or any(c not in "0123456789abcdef" for c in lock_hash):
        raise ContractError("ENVIRONMENT_UNAVAILABLE", "The packaged dependency lock is invalid.")
    identity = {
        "source_sha256": _digest(sources),
        "dependency_lock_sha256": lock_hash,
        "project_sha256": hashlib.sha256(
            package.joinpath("runtime-pyproject.toml").read_bytes()
        ).hexdigest(),
        "python": platform.python_version(),
        "psycopg": metadata.version("psycopg"),
    }
    return {**identity, "implementation_sha256": _digest(identity)}


def shift_month(month: date, offset: int) -> date:
    if type(month) is not date or month.day != 1:
        raise ContractError("INVALID_MONTH", "Report months must be calendar month starts.")
    index = (month.year - 1) * 12 + month.month - 1 + offset
    if not 0 <= index < 9999 * 12:
        raise ContractError("INVALID_GRID", "Required calendar lookback exceeds the date domain.")
    return date(index // 12 + 1, index % 12 + 1, 1)


def required_months(formula: str, output_months: tuple[date, ...]) -> tuple[date, ...]:
    if not isinstance(formula, str) or formula not in FORMULAS:
        raise ContractError("UNKNOWN_FORMULA", "Select a supported fixed monthly formula.")
    if (
        not isinstance(output_months, tuple)
        or not output_months
        or len(output_months) > MAX_OUTPUT_MONTHS
    ):
        raise ContractError("INVALID_GRID", "Request 1 to 2400 explicit report months.")
    for month in output_months:
        shift_month(month, 0)
    if len(set(output_months)) != len(output_months):
        raise ContractError("INVALID_GRID", "Report months must not contain duplicates.")
    return tuple(
        sorted(
            {
                shift_month(month, offset)
                for month in output_months
                for offset in range(-FORMULAS[formula], 1)
            }
        )
    )


def render_status(
    mode: str,
    storage_status: str,
    source_status: str | None,
    unit_compatible: bool | None,
) -> dict:
    """Apply the two accepted display policies without changing stored selection."""
    display_status = storage_status
    warnings = []
    if storage_status == "unit_mismatch" and source_status in ("missing", "withdrawn"):
        display_status = source_status
    if unit_compatible is False:
        warnings.append("unit_mismatch")
    messages = {
        "value": ("VALUE", "Value available"),
        "withdrawn": ("SOURCE_WITHDRAWN", "Source withdrew this value"),
        "missing": ("SOURCE_MISSING", "Source marks this value missing"),
        "unknown_availability": (
            "UNKNOWN_AVAILABILITY",
            "Release date unknown; historical value cannot be established",
        ),
        "unit_mismatch": ("UNIT_MISMATCH", "Unit or metadata mismatch"),
        "incompatible_schema": ("INCOMPATIBLE_SCHEMA", "Incompatible schema"),
        "not_available_at_cutoff": (
            ("NO_DATA_BEFORE_CUTOFF", "No data before the cutoff")
            if mode == "source_as_of"
            else ("NO_RETAINED_DATA", "No data in this snapshot")
        ),
    }
    if display_status not in messages:
        raise ContractError("INVALID_EVIDENCE", "Unsupported evidence status.")
    message_code, message_zh = messages[display_status]
    return {
        "storage_status": storage_status,
        "display_status": display_status,
        "message_code": message_code,
        "message_zh": message_zh,
        "warnings": warnings,
    }


def compose_rows(
    formula: str,
    output_months: tuple[date, ...],
    evidence: list[dict],
    *,
    _reused_values: dict[str, str] | None = None,
) -> list[dict]:
    """Compute complete calendar windows; unavailable slots remain in each row."""
    expected = required_months(formula, output_months)
    indexed = {item["observation_month"]: item for item in evidence}
    if len(indexed) != len(evidence) or set(indexed) != {m.isoformat() for m in expected}:
        raise ContractError(
            "INVALID_EVIDENCE", "Evidence must cover the exact calendar input grid."
        )
    scope_keys = (
        "dataset_id",
        "snapshot_id",
        "source_namespace",
        "series_key",
        "mode",
        "exclusive_end_date",
    )
    scopes = {tuple(item.get(key) for key in scope_keys) for item in evidence}
    if len(scopes) != 1:
        raise ContractError("INVALID_EVIDENCE", "All formula inputs must share one pinned scope.")
    rows = []
    priority = {
        name: i
        for i, name in enumerate(
            (
                "unknown_availability",
                "withdrawn",
                "missing",
                "incompatible_schema",
                "unit_mismatch",
                "not_available_at_cutoff",
            )
        )
    }
    for month in output_months:
        inputs = []
        for offset in range(-FORMULAS[formula], 1):
            item = indexed[shift_month(month, offset).isoformat()]
            display = render_status(
                item["mode"], item["status"], item["source_status"], item["unit_compatible"]
            )
            inputs.append({**item, "display": display})
        reasons = [
            {"observation_month": item["observation_month"], **item["display"]}
            for item in inputs
            if item["status"] != "value"
        ]
        units = [item["unit"] for item in inputs]
        compatible = units[0] is not None and all(unit == units[0] for unit in units)
        warnings = sorted({warning for item in inputs for warning in item["display"]["warnings"]})
        if any(item["availability_unknown"] for item in inputs):
            warnings.append("unknown_source_availability")
        if not reasons and not compatible:
            reasons.append(
                {
                    "observation_month": month.isoformat(),
                    **render_status(inputs[0]["mode"], "unit_mismatch", None, False),
                }
            )
            if "unit_mismatch" not in warnings:
                warnings.append("unit_mismatch")
        status = (
            min((r["display_status"] for r in reasons), key=priority.__getitem__)
            if reasons
            else "value"
        )
        value = None
        if not reasons:
            if _reused_values is not None and month.isoformat() in _reused_values:
                value = format_scaled(parse_scaled(_reused_values[month.isoformat()]))
            else:
                numbers = [parse_scaled(item["value"]) for item in inputs]
                if formula == "employment_delta":
                    value = format_scaled(numbers[1] - numbers[0])
                else:
                    value = canonical_ratio(sum(numbers), 3 * SCALE)
        rows.append(
            {
                "observation_month": month.isoformat(),
                "status": status,
                "value": value,
                "unit": units[0] if compatible else None,
                "warnings": sorted(set(warnings)),
                "reasons": reasons,
                "inputs": inputs,
            }
        )
    return rows


def _context(conn: psycopg.Connection, context_id: UUID) -> dict:
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT * FROM chronicle.analysis_context WHERE context_id=%s", (context_id,)
        )
        context = cursor.fetchone()
    if context is None:
        raise ContractError("CONTEXT_NOT_FOUND", "No retained context has that identity.")
    return context


def _stored(conn: psycopg.Connection, report_id: UUID) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT * FROM chronicle.report_run WHERE report_id=%s", (report_id,))
        record = cursor.fetchone()
    if record and _digest(record["document"]) != record["document_sha256"]:
        raise ContractError("REPORT_CORRUPT", "Retained report digest does not match its content.")
    return record


def replay_report(conn: psycopg.Connection, report_id: UUID) -> dict:
    """Retrieve original canonical output, without executing the formula or model."""
    record = _stored(conn, report_id)
    if record is None:
        raise ContractError("REPORT_NOT_FOUND", "No retained report has that identity.")
    return record["document"]


def build_report(
    conn: psycopg.Connection,
    context_id: UUID,
    formula: str,
    *,
    output_months: tuple[date, ...] | None = None,
    report_id: UUID | None = None,
) -> dict:
    return _build_report(
        conn, context_id, formula, output_months=output_months, report_id=report_id
    )


def _build_report(
    conn: psycopg.Connection,
    context_id: UUID,
    formula: str,
    *,
    output_months: tuple[date, ...] | None = None,
    report_id: UUID | None = None,
    row_builder=None,
    execution: dict | None = None,
) -> dict:
    """Atomically retain all rows and evidence, or leave no report/halo context."""
    report_id = report_id or uuid4()
    if not isinstance(report_id, UUID) or not isinstance(context_id, UUID):
        raise ContractError("INVALID_IDENTITY", "Report and context identities must be UUIDs.")
    with conn.transaction():
        # Serialize identical report identities before creating their halo contexts.
        # Hash collisions only serialize unrelated requests; they cannot merge identity.
        lock_key = int.from_bytes(report_id.bytes[:8], "big", signed=True)
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        context = _context(conn, context_id)
        months = tuple(context["months"]) if output_months is None else output_months
        halo = required_months(formula, months)
        if not set(months).issubset(context["months"]):
            raise ContractError("INVALID_GRID", "Output months must belong to the base context.")
        identity = implementation_identity()
        request = {
            "context_id": str(context_id),
            "formula": formula,
            "formula_version": formula + ".v1",
            "output_months": [m.isoformat() for m in months],
            "numeric_policy": NUMERIC_POLICY,
            "report_schema": REPORT_SCHEMA,
            "unit_policy": "exact-name-and-metadata-v1",
            "implementation": identity,
        }
        if execution is not None:
            request["execution"] = execution
        request_hash = _digest(request)
        previous = _stored(conn, report_id)
        if previous is not None:
            if previous["request_sha256"] != request_hash:
                raise ContractError(
                    "REPORT_IDENTITY_CONFLICT", "Report identity binds another request."
                )
            return previous["document"]
        input_context_id = uuid5(report_id, "chronicle.report.inputs.v1")
        query = Query(
            context["mode"],
            context["snapshot_id"],
            context["source_namespace"],
            context["series_key"],
            halo,
            SourceCutoff(context["exclusive_end_date"]) if context["exclusive_end_date"] else None,
            Unit(context["expected_unit_name"], context["expected_metadata_version"])
            if context["expected_unit_name"]
            else None,
        )
        create_context(conn, context["dataset_id"], query, input_context_id)
        evidence = select_evidence(conn, input_context_id)
        diagnostics = None
        if row_builder is None:
            rows = compose_rows(formula, months, evidence)
        else:
            rows, diagnostics = row_builder(context, formula, months, evidence)
        document = {
            **request,
            "report_id": str(report_id),
            "input_context_id": str(input_context_id),
            "dataset_id": context["dataset_id"],
            "snapshot_id": context["snapshot_id"],
            "mode": context["mode"],
            "exclusive_end_date": context["exclusive_end_date"].isoformat()
            if context["exclusive_end_date"]
            else None,
            "source_namespace": context["source_namespace"],
            "series_key": context["series_key"],
            "expected_unit": {
                "name": context["expected_unit_name"],
                "metadata_version": context["expected_metadata_version"],
            }
            if context["expected_unit_name"]
            else None,
            "evidence_schema": "chronicle.evidence.v1",
            "rows": rows,
        }
        if diagnostics is not None:
            document["execution_result"] = diagnostics
        if len(canonical_json(document)) > MAX_DOCUMENT_BYTES:
            raise ContractError("REPORT_TOO_LARGE", "Report exceeds the retained document limit.")
        conn.execute(
            "INSERT INTO chronicle.report_run(report_id,context_id,input_context_id,request_sha256,"
            "implementation_sha256,dependency_lock_sha256,document_sha256,document) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                report_id,
                context_id,
                input_context_id,
                request_hash,
                identity["implementation_sha256"],
                identity["dependency_lock_sha256"],
                _digest(document),
                Jsonb(document),
            ),
        )
    return document


def reexecute_report(conn: psycopg.Connection, report_id: UUID) -> dict:
    """Verify current execution equals retained rows; never silently upgrade code."""
    document = replay_report(conn, report_id)
    if document["implementation"] != implementation_identity():
        raise ContractError(
            "ENVIRONMENT_UNAVAILABLE", "Recorded executable environment is not active."
        )
    rows = compose_rows(
        document["formula"],
        tuple(date.fromisoformat(m) for m in document["output_months"]),
        select_evidence(conn, UUID(document["input_context_id"])),
    )
    if rows != document["rows"]:
        raise ContractError(
            "REPORT_REEXECUTION_MISMATCH", "Execution differs from the retained report."
        )
    return {"report_id": str(report_id), "status": "verified", "document": document}
