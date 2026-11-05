"""Versioned, snapshot-scoped input evidence, separate from legacy selection."""

import json
from importlib.resources import files
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from chronicle.errors import ContractError

EVIDENCE_SCHEMA = "chronicle.evidence.v1"
MAX_OCCURRENCES_PER_REVISION = 100
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
SELECT_EVIDENCE_SQL = files("chronicle").joinpath("sql/select_evidence_v1.sql").read_text()


def select_evidence(conn: psycopg.Connection, context_id: UUID) -> list[dict]:
    """Return every pinned input slot, including absence and blocking uncertainty.

    Only ``value`` is authoritative numeric input. ``blocking_revision`` is
    diagnostic evidence and must never be substituted for an unavailable value.
    Receipt timestamps use UTC with six fractional digits; matching occurrences
    are ordered by UTF-8 batch identifier bytes, then receipt instant. More than
    100 matching receipts for a selected/blocking revision or more than 4 MiB of
    canonical evidence JSON rejects the entire request. A server cursor fetches
    one bounded slot at a time; no truncated or partial response is returned.
    """
    records = []
    total_bytes = 2  # Opening and closing brackets of the canonical JSON array.
    with conn.transaction():
        if not conn.execute(
            "SELECT 1 FROM chronicle.analysis_context WHERE context_id=%s", (context_id,)
        ).fetchone():
            raise ContractError("CONTEXT_NOT_FOUND", "no retained context with that identity")
        # A normal client cursor buffers the whole PostgreSQL result even when
        # Python iterates rows. A named cursor bounds each network fetch as well.
        with conn.cursor(name=f"evidence_{uuid4().hex}", row_factory=dict_row) as cursor:
            cursor.itersize = 1
            cursor.execute(
                SELECT_EVIDENCE_SQL,
                {"context_id": context_id, "occurrence_limit": MAX_OCCURRENCES_PER_REVISION + 1},
            )
            for row in cursor:
                for key in ("selected_revision", "blocking_revision"):
                    revision = row[key]
                    if revision and len(revision["occurrences"]) > MAX_OCCURRENCES_PER_REVISION:
                        raise ContractError(
                            "EVIDENCE_TOO_LARGE",
                            "A selected revision exceeds the 100 matching-receipt service limit.",
                        )
                row["observation_month"] = row["observation_month"].isoformat()
                if row["exclusive_end_date"] is not None:
                    row["exclusive_end_date"] = row["exclusive_end_date"].isoformat()
                total_bytes += len(
                    json.dumps(
                        row,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ) + bool(records)
                if total_bytes > MAX_EVIDENCE_BYTES:
                    raise ContractError(
                        "EVIDENCE_TOO_LARGE", "Evidence exceeds the 4 MiB canonical JSON limit."
                    )
                records.append(row)
    return records
