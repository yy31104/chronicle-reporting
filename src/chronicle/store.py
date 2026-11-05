"""Explicit PostgreSQL transactions for synthetic batch import and pinned queries."""

import hashlib
import json
from dataclasses import asdict
from datetime import date
from importlib.resources import files
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from chronicle.admission import RevisionCatalog
from chronicle.domain import KnowledgeMode, Query, Revision, SourceCutoff, Unit
from chronicle.errors import ContractError, database_errors

# Loaded SQL remains separate from the independent test oracle.
SELECT_SQL = files("chronicle").joinpath("sql/select_context.sql").read_text()


def create_dataset(conn: psycopg.Connection, dataset_id: str) -> None:
    with database_errors(), conn.transaction():
        conn.execute(
            "INSERT INTO chronicle.dataset(dataset_id) VALUES (%s) "
            "ON CONFLICT (dataset_id) DO NOTHING",
            (dataset_id,),
        )


def _batch_fingerprint(revisions: tuple[Revision, ...]) -> str:
    canonical_rows = []
    for revision in revisions:
        row = asdict(revision)
        row["occurrences"] = sorted(row["occurrences"], key=lambda item: item["received_at"])
        canonical_rows.append(json.dumps(row, default=str, sort_keys=True, separators=(",", ":")))
    return hashlib.sha256("\n".join(sorted(canonical_rows)).encode()).hexdigest()


def import_batch(
    conn: psycopg.Connection, dataset_id: str, batch_id: str, revisions: tuple[Revision, ...]
) -> str:
    """Insert a complete batch atomically; exact retries return the same batch ID.

    All occurrences must belong to this batch. Accepted revision values are never
    overwritten. A rejected transaction leaves no partial batch or fake quarantine.
    """
    catalog = RevisionCatalog(revisions)
    occurrences = [receipt for row in catalog.revisions for receipt in row.occurrences]
    if any(receipt.batch_id != batch_id for receipt in occurrences):
        raise ContractError("BATCH_MISMATCH", "receipts must belong to the imported batch")
    fingerprint = _batch_fingerprint(revisions)
    with database_errors(), conn.transaction():
        claimed = conn.execute(
            "INSERT INTO chronicle.import_batch "
            "(dataset_id, batch_id, content_sha256, expected_occurrence_count) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (dataset_id, batch_id) DO NOTHING "
            "RETURNING batch_id",
            (dataset_id, batch_id, fingerprint, len(occurrences)),
        ).fetchone()
        if claimed is None:
            previous = conn.execute(
                "SELECT content_sha256, state FROM chronicle.import_batch "
                "WHERE dataset_id=%s AND batch_id=%s",
                (dataset_id, batch_id),
            ).fetchone()
            if previous != (fingerprint, "validated"):
                raise ContractError(
                    "BATCH_CONFLICT", "batch identity already has different content/state"
                )
            return batch_id
        for row in sorted(
            revisions,
            key=lambda item: (
                item.source_namespace,
                item.source_revision_key,
                item.series_key,
                item.observation_month,
            ),
        ):
            _insert_revision(conn, dataset_id, row)
            for receipt in row.occurrences:
                conn.execute(
                    "INSERT INTO chronicle.revision_occurrence "
                    "(dataset_id,batch_id,source_namespace,series_key,observation_month,"
                    "source_revision_key,received_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        dataset_id,
                        batch_id,
                        row.source_namespace,
                        row.series_key,
                        row.observation_month,
                        row.source_revision_key,
                        receipt.received_at,
                    ),
                )
        conn.execute("SELECT chronicle.seal_batch(%s,%s)", (dataset_id, batch_id))
    return batch_id


def _insert_revision(conn: psycopg.Connection, dataset_id: str, row: Revision) -> None:
    scope = (dataset_id, row.source_namespace)
    conn.execute(
        "INSERT INTO chronicle.source_namespace(dataset_id,source_namespace,order_rule) "
        "VALUES (%s,%s,%s) ON CONFLICT (dataset_id,source_namespace) DO NOTHING",
        (*scope, row.order_rule.value),
    )
    rule = conn.execute(
        "SELECT order_rule FROM chronicle.source_namespace "
        "WHERE dataset_id=%s AND source_namespace=%s",
        scope,
    ).fetchone()
    if rule != (row.order_rule.value,):
        raise ContractError("AMBIGUOUS_SOURCE_ORDER", "source namespace order rules differ")
    conn.execute(
        "INSERT INTO chronicle.series(dataset_id,source_namespace,series_key) VALUES (%s,%s,%s) "
        "ON CONFLICT (dataset_id,source_namespace,series_key) DO NOTHING",
        (*scope, row.series_key),
    )
    release = (
        row.source_order,
        row.order_rule.value,
        row.availability.precision.value,
        row.availability.date,
    )
    conn.execute(
        "INSERT INTO chronicle.source_release "
        "(dataset_id,source_namespace,source_revision_key,source_order,order_rule,"
        "availability_precision,release_day) VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (dataset_id,source_namespace,source_revision_key) DO NOTHING",
        (*scope, row.source_revision_key, *release),
    )
    stored = conn.execute(
        "SELECT source_order,order_rule,availability_precision,release_day "
        "FROM chronicle.source_release "
        "WHERE dataset_id=%s AND source_namespace=%s AND source_revision_key=%s",
        (*scope, row.source_revision_key),
    ).fetchone()
    if stored != release:
        raise ContractError("RELEASE_CONFLICT", "release identity has different source evidence")
    key = (*scope, row.series_key, row.observation_month, row.source_revision_key)
    content = (row.status.value, row.value, row.unit.name, row.unit.metadata_version)
    conn.execute(
        "INSERT INTO chronicle.revision "
        "(dataset_id,source_namespace,series_key,observation_month,source_revision_key,"
        "status,value,unit_name,metadata_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT "
        "(dataset_id,source_namespace,series_key,observation_month,source_revision_key) "
        "DO NOTHING",
        (*key, *content),
    )
    stored = conn.execute(
        "SELECT status,value,unit_name,metadata_version FROM chronicle.revision "
        "WHERE dataset_id=%s AND source_namespace=%s AND series_key=%s AND observation_month=%s "
        "AND source_revision_key=%s",
        key,
    ).fetchone()
    if stored != content:
        raise ContractError("REVISION_CONFLICT", "revision identity has different lexical content")


def publish_snapshot(
    conn: psycopg.Connection,
    dataset_id: str,
    snapshot_id: str,
    batch_ids,
    manifest_sha256: str,
    expected_current: str | None,
) -> str:
    with database_errors(), conn.transaction():
        return conn.execute(
            "SELECT chronicle.publish_snapshot(%s,%s,%s,%s,%s)",
            (dataset_id, snapshot_id, sorted(batch_ids), manifest_sha256, expected_current),
        ).fetchone()[0]


def _create_context(
    conn: psycopg.Connection,
    dataset_id: str,
    query: Query,
    snapshot_id: str | None,
    context_id: UUID | None,
) -> UUID:
    with database_errors(), conn.transaction():
        return conn.execute(
            "SELECT chronicle.create_context(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                context_id or uuid4(),
                dataset_id,
                snapshot_id,
                query.mode.value,
                query.source_namespace,
                query.series_key,
                list(query.months),
                query.cutoff.exclusive_end_date if query.cutoff else None,
                query.expected_unit.name if query.expected_unit else None,
                query.expected_unit.metadata_version if query.expected_unit else None,
            ),
        ).fetchone()[0]


def create_context(
    conn: psycopg.Connection,
    dataset_id: str,
    query: Query,
    context_id: UUID | None = None,
) -> UUID:
    return _create_context(conn, dataset_id, query, query.snapshot_id, context_id)


def resolve_current_context(
    conn: psycopg.Connection,
    dataset_id: str,
    mode: KnowledgeMode,
    source_namespace: str,
    series_key: str,
    months: tuple[date, ...],
    cutoff: SourceCutoff | None = None,
    expected_unit: Unit | None = None,
    context_id: UUID | None = None,
) -> UUID:
    # A temporary sentinel validates all non-snapshot fields; the database resolves
    # its actual pointer once and stores that ID in an immutable context.
    query = Query(
        mode, "resolve-current", source_namespace, series_key, months, cutoff, expected_unit
    )
    if query.mode is KnowledgeMode.SYSTEM_SNAPSHOT:
        raise ContractError("SNAPSHOT_REQUIRED", "system_snapshot needs an explicit snapshot ID")
    return _create_context(conn, dataset_id, query, None, context_id)


def select_context(conn: psycopg.Connection, context_id: UUID) -> list[dict]:
    with database_errors(), conn.transaction():
        if not conn.execute(
            "SELECT 1 FROM chronicle.analysis_context WHERE context_id=%s", (context_id,)
        ).fetchone():
            raise ContractError("CONTEXT_NOT_FOUND", "no retained context with that identity")
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute(SELECT_SQL, {"context_id": context_id})
            records = cursor.fetchall()
    result = []
    for row in records:
        unit_name = row.pop("unit_name")
        metadata_version = row.pop("metadata_version")
        row["observation_month"] = row["observation_month"].isoformat()
        row["unit"] = (
            {"name": unit_name, "metadata_version": metadata_version} if unit_name else None
        )
        result.append(row)
    return result
