"""Resumable direct-source admission and explicit immutable parser generations."""

import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from chronicle import objects, sources, store
from chronicle.domain import Occurrence, Revision, SourceAvailability, Unit
from chronicle.domain import _identifier as validate_identifier
from chronicle.errors import ContractError, database_errors

_INGEST_CODES = {
    "UNKNOWN_DATASET",
    "INGESTION_IDENTITY_CONFLICT",
    "PARSER_GENERATION_REQUIRED",
    "GENERATION_ALREADY_EXISTS",
    "GENERATION_NOT_EMPTY",
    "INGESTION_NOT_FOUND",
    "INGESTION_OBJECT_MISMATCH",
}


@contextmanager
def _errors():
    with database_errors():
        try:
            yield
        except psycopg.Error as exc:
            if (exc.sqlstate, exc.diag.schema_name, exc.diag.table_name) == (
                "P0001",
                "chronicle",
                "ingestion_request",
            ):
                for code in _INGEST_CODES:
                    if exc.diag.constraint_name == "ingest_" + code.lower():
                        raise ContractError(code, code) from None
            raise


def parser_identity() -> str:
    """A changed parser requires a new dataset generation, never revised old rows."""
    return hashlib.sha256(
        sources.PARSER_VERSION.encode()
        + b"\0"
        + files("chronicle").joinpath("sources.py").read_bytes()
    ).hexdigest()


@contextmanager
def _request_lock(importer, publisher, request_id):
    if not isinstance(request_id, UUID):
        raise ContractError("INVALID_IDENTITY", "Ingestion needs a caller-retained UUID")
    for conn in (importer, publisher):
        if not conn.autocommit or conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            raise ContractError("INGESTION_TRANSACTION_ACTIVE", "Ingestion needs idle connections")
    if importer.info.dbname != publisher.info.dbname:
        raise ContractError("DATABASE_MISMATCH", "Ingestion roles must use the same database")
    key = int.from_bytes(hashlib.sha256(request_id.bytes).digest()[:4], signed=True)
    publisher.execute("SELECT pg_advisory_lock(943791,%s)", (key,))
    try:
        yield
    finally:
        if not publisher.closed:
            publisher.execute("SELECT pg_advisory_unlock(943791,%s)", (key,))


def _claim(importer, publisher, request_id, dataset_id, document):
    prior = publisher.execute(
        "SELECT r.request_document FROM chronicle.ingestion_request r "
        "JOIN chronicle.artifact_publication p ON p.request_id=r.request_id WHERE r.request_id=%s",
        (request_id,),
    ).fetchone()
    if prior:
        # Completed identity replay needs no executable parser. Preserve its code
        # binding while comparing every caller-controlled request field normally.
        document = {**document, "parser_identity": prior[0]["parser_identity"]}
    store.create_dataset(importer, dataset_id)
    with _errors(), publisher.transaction():
        publisher.execute(
            "SELECT chronicle.claim_ingestion(%s,%s,%s,%s,%s,%s,%s)",
            (
                request_id,
                dataset_id,
                Jsonb(document),
                document["parser_identity"],
                document.get("predecessor_dataset"),
                document.get("predecessor_snapshot"),
                document.get("defect_note", "Initial direct-source interpretation"),
            ),
        )
    generation = publisher.execute(
        "SELECT interpreted_at FROM chronicle.interpretation_generation WHERE dataset_id=%s",
        (dataset_id,),
    ).fetchone()[0]
    return generation


def _replay(publisher, request_id, dataset_id, snapshot_id, root):
    found = publisher.execute(
        "SELECT snapshot_id FROM chronicle.artifact_publication WHERE request_id=%s",
        (request_id,),
    ).fetchone()
    if found:
        if found[0] != snapshot_id:
            raise ContractError("INGESTION_IDENTITY_CONFLICT", "Stored publication differs")
        objects.require_snapshot_access(publisher, dataset_id, snapshot_id, root=root)
        for row in publisher.execute(
            "SELECT ticket_id,worker_id,object_sha256,policy_sha256 "
            "FROM chronicle.ingestion_acquisition WHERE request_id=%s",
            (request_id,),
        ).fetchall():
            objects.end_ticket(publisher, objects.StagedObject(*row))
        return snapshot_id
    return None


def _record(publisher, request_id, source_id, artifact, staged):
    revisions = json.loads(json.dumps([asdict(row) for row in artifact.revisions], default=str))
    document = {"manifest": artifact.manifest, "revisions": revisions}
    with _errors(), publisher.transaction():
        publisher.execute(
            "SELECT chronicle.record_ingestion_acquisition(%s,%s,%s,%s,%s,%s,%s)",
            (
                request_id,
                source_id,
                staged.ticket_id,
                staged.worker_id,
                staged.sha256,
                staged.policy_sha256,
                Jsonb(document),
            ),
        )


def _acquisition(publisher, request_id, source_id):
    return publisher.execute(
        "SELECT ticket_id,worker_id,object_sha256,policy_sha256,source_document "
        "FROM chronicle.ingestion_acquisition WHERE request_id=%s AND source_id=%s",
        (request_id, source_id),
    ).fetchone()


def _revisions(document):
    return tuple(
        Revision(
            source_namespace=row["source_namespace"],
            series_key=row["series_key"],
            observation_month=date.fromisoformat(row["observation_month"]),
            source_revision_key=row["source_revision_key"],
            source_order=row["source_order"],
            order_rule=row["order_rule"],
            availability=SourceAvailability(
                row["availability"]["precision"],
                date.fromisoformat(row["availability"]["date"])
                if row["availability"]["date"]
                else None,
            ),
            value=row["value"],
            status=row["status"],
            unit=Unit(**row["unit"]),
            occurrences=tuple(
                Occurrence(r["batch_id"], datetime.fromisoformat(r["received_at"]))
                for r in row["occurrences"]
            ),
        )
        for row in document["revisions"]
    )


def _finish(
    importer,
    publisher,
    request_id,
    dataset_id,
    snapshot_id,
    source_ids,
    expected_parent,
    root,
    acquire,
    _fault,
):
    tickets, batches = [], []
    for source_id in source_ids:
        batch = f"source-{request_id.hex}-{source_id}"
        stored = _acquisition(publisher, request_id, source_id)
        if stored is None:
            artifact = acquire(source_id, batch)
            sources.validate_release_order((artifact,))
            staged = objects.stage_bytes(
                publisher,
                dataset_id,
                artifact.raw,
                artifact.manifest["rights_policy"],
                artifact.manifest,
                root=root,
            )
            _record(publisher, request_id, source_id, artifact, staged)
            if _fault:
                _fault("acquisition_retained")
            stored = _acquisition(publisher, request_id, source_id)
        staged = objects.StagedObject(*stored[:4])
        objects.read_staged_object(publisher, staged, root=root)
        rows = _revisions(stored[4])
        store.import_batch(importer, dataset_id, batch, rows)
        if _fault:
            _fault("batch_committed")
        objects.attach_batch_object(publisher, dataset_id, batch, staged)
        tickets.append(staged)
        batches.append(batch)
    result = objects.publish_snapshot(
        publisher,
        dataset_id,
        snapshot_id,
        batches,
        expected_parent,
        request_id,
        root=root,
    )
    if _fault:
        _fault("publication_committed")
    for staged in tickets:
        objects.end_ticket(publisher, staged)
    return result


def import_sources(
    importer: psycopg.Connection,
    publisher: psycopg.Connection,
    dataset_id: str,
    snapshot_id: str,
    source_ids: tuple[str, ...],
    expected_parent: str | None,
    request_id: UUID,
    *,
    root: Path | None = None,
    local_files: dict[str, Path] | None = None,
    _fault=None,
) -> str:
    """Fetch, durably retain, import, attach and atomically publish all requested sources.

    Keep request_id for retries. Captured bytes, parsed rows and receipts are reused
    after a crash; successful replay never refetches or moves the current pointer.
    A changed request or parser is explicitly rejected. No failed page is fabricated.
    Optional local files are explicitly operator-asserted direct-publisher HTML,
    never a claim that this service fetched/authenticated the remote source.
    """
    validate_identifier(dataset_id, "dataset_id")
    validate_identifier(snapshot_id, "snapshot_id")
    if expected_parent is not None:
        validate_identifier(expected_parent, "expected_parent")
    if (
        not isinstance(source_ids, tuple)
        or not source_ids
        or len(source_ids) > 4
        or any(not isinstance(source, str) for source in source_ids)
        or len(set(source_ids)) != len(source_ids)
        or any(source not in sources.SOURCES for source in source_ids)
    ):
        raise ContractError(
            "SOURCE_NOT_ALLOWED", "Choose unique IDs from the four-source allowlist"
        )
    source_ids = tuple(sorted(source_ids))
    if local_files is not None and (
        not isinstance(local_files, dict) or not set(local_files).issubset(source_ids)
    ):
        raise ContractError("SOURCE_NOT_ALLOWED", "Saved sources must be in the requested IDs")
    saved = {
        source_id: sources.read_saved_source(source_id, path)
        for source_id, path in (local_files or {}).items()
    }
    document = {
        "schema": "chronicle.ingestion.v1",
        "kind": "direct_source",
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "source_ids": list(source_ids),
        "expected_parent": expected_parent,
        "parser_identity": parser_identity(),
    }
    if saved:
        document["saved_sources"] = {
            key: {
                "method": "local_file",
                "source_uri": sources.SOURCES[key].url,
                "raw_sha256": item.sha256,
                "authenticity": "operator_asserted_not_independently_authenticated",
            }
            for key, item in sorted(saved.items())
        }

    def acquire(source_id, batch_id):
        if source_id in saved:
            return sources.parse_saved_source(saved[source_id], batch_id)
        return sources.fetch_and_parse(source_id, batch_id)

    with _request_lock(importer, publisher, request_id):
        _claim(importer, publisher, request_id, dataset_id, document)
        if replay := _replay(publisher, request_id, dataset_id, snapshot_id, root):
            return replay
        return _finish(
            importer,
            publisher,
            request_id,
            dataset_id,
            snapshot_id,
            source_ids,
            expected_parent,
            root,
            acquire,
            _fault,
        )


def repair_generation(
    importer: psycopg.Connection,
    publisher: psycopg.Connection,
    predecessor_dataset: str,
    predecessor_snapshot: str,
    dataset_id: str,
    snapshot_id: str,
    defect_note: str,
    request_id: UUID,
    *,
    root: Path | None = None,
    _fault=None,
) -> str:
    """Reinterpret verified original bytes in a new immutable dataset generation.

    The original raw receipt is preserved; the generation has a separate actual
    reinterpretation timestamp. Existing normalized data/reports remain untouched.
    """
    for value, name in (
        (dataset_id, "dataset_id"),
        (snapshot_id, "snapshot_id"),
        (predecessor_dataset, "predecessor_dataset"),
        (predecessor_snapshot, "predecessor_snapshot"),
    ):
        validate_identifier(value, name)
    if dataset_id == predecessor_dataset:
        raise ContractError("GENERATION_REQUIRED", "Parser repair requires a new dataset")
    if not isinstance(defect_note, str) or not 1 <= len(defect_note.strip()) <= 4000:
        raise ContractError("DEFECT_NOTE_REQUIRED", "Record a bounded parser defect explanation")
    document = {
        "schema": "chronicle.ingestion.v1",
        "kind": "parser_repair",
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "expected_parent": None,
        "predecessor_dataset": predecessor_dataset,
        "predecessor_snapshot": predecessor_snapshot,
        "defect_note": defect_note.strip(),
        "parser_identity": parser_identity(),
    }
    with _request_lock(importer, publisher, request_id):
        objects.require_snapshot_access(
            publisher, predecessor_dataset, predecessor_snapshot, "local_ingest", root=root
        )
        interpreted_at = _claim(importer, publisher, request_id, dataset_id, document)
        if replay := _replay(publisher, request_id, dataset_id, snapshot_id, root):
            return replay
        rows = publisher.execute(
            "SELECT a.source_metadata,a.object_sha256 FROM chronicle.snapshot_batch s "
            "JOIN chronicle.batch_artifact a ON a.dataset_id=s.dataset_id "
            "AND a.batch_id=s.batch_id "
            "WHERE s.dataset_id=%s AND s.snapshot_id=%s "
            'ORDER BY a.batch_id COLLATE "C",a.object_sha256 COLLATE "C"',
            (predecessor_dataset, predecessor_snapshot),
        ).fetchall()
        originals = {}
        for metadata, digest in rows:
            source_id = metadata.get("source_id")
            if (
                source_id not in sources.SOURCES
                or metadata.get("schema_version") != "chronicle.source.v1"
            ):
                raise ContractError(
                    "UNSUPPORTED_REPAIR_SOURCE", "Only retained direct sources can be repaired"
                )
            originals.setdefault(source_id, (metadata, digest))
        if not originals:
            raise ContractError("UNSUPPORTED_REPAIR_SOURCE", "No retained source artifacts")

        def acquire(source_id, batch):
            metadata, digest = originals[source_id]
            raw = objects.read_object(
                publisher,
                predecessor_dataset,
                predecessor_snapshot,
                digest,
                "local_ingest",
                root=root,
            )
            artifact = sources.parse_payload(
                source_id,
                raw,
                datetime.fromisoformat(metadata["received_at"]),
                batch,
                policy=metadata["rights_policy"],
            )
            # A new interpretation does not authenticate operator-supplied bytes
            # or change the historical acquisition method of the original.
            artifact.manifest["acquisition_channel"] = metadata["acquisition_channel"]
            if "acquisition" in metadata:
                artifact.manifest["acquisition"] = metadata["acquisition"]
            artifact.manifest["interpretation"] = {
                "kind": "parser_repair",
                "predecessor_dataset": predecessor_dataset,
                "predecessor_snapshot": predecessor_snapshot,
                "original_raw_sha256": digest,
                "original_parser_version": metadata["parser_version"],
                "original_received_at": metadata["received_at"],
                "interpreted_at": interpreted_at.isoformat(),
                "defect_note": defect_note.strip(),
            }
            return artifact

        return _finish(
            importer,
            publisher,
            request_id,
            dataset_id,
            snapshot_id,
            tuple(sorted(originals)),
            None,
            root,
            acquire,
            _fault,
        )
