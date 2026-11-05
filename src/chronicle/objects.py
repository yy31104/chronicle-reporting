"""Durable local objects, cumulative publication, live rights and conservative GC.

The configured root is exclusively service-owned on a local filesystem. Published
bytes are never overwritten or automatically deleted. No TTL ends a live worker.
"""

import hashlib
import io
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from chronicle.errors import ContractError, database_errors

MAX_OBJECT_BYTES = 32 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}")
_OBJECT_CODES = {
    "RIGHTS_DENIED",
    "TICKET_IDENTITY_CONFLICT",
    "TICKET_NOT_ACTIVE",
    "OBJECT_IDENTITY_CONFLICT",
    "OBJECT_NOT_READY",
    "TICKET_NOT_OWNED",
    "ARTIFACT_IDENTITY_CONFLICT",
    "BATCH_ARTIFACT_CLOSED",
    "DATASET_NOT_FOUND",
    "PUBLICATION_IDENTITY_CONFLICT",
    "SNAPSHOT_BASE_CHANGED",
    "INVALID_PUBLICATION",
    "ARTIFACT_CLOSURE_INCOMPLETE",
    "MANIFEST_MISMATCH",
    "POLICY_NOT_FOUND",
    "INVALID_GRACE_PERIOD",
    "OBJECT_NOT_DELETING",
    "PIN_NOT_OWNED",
}
_MANIFEST_RIGHTS = {
    "policy_id": "chronicle-generated-manifest",
    "version": "1",
    "decision_state": "approved_scoped",
    "reviewer": "Chronicle service",
    "scope": "Generated structural snapshot manifest; source rights are checked separately.",
    "permitted_uses": ["local_ingest", "local_analysis"],
}


@dataclass(frozen=True)
class StagedObject:
    ticket_id: UUID
    worker_id: UUID
    sha256: str
    policy_sha256: str


@contextmanager
def _errors():
    with database_errors():
        try:
            yield
        except psycopg.Error as exc:
            if (exc.sqlstate, exc.diag.schema_name, exc.diag.table_name) == (
                "P0001",
                "chronicle",
                "storage_object",
            ):
                for code in _OBJECT_CODES:
                    if exc.diag.constraint_name == "object_" + code.lower():
                        raise ContractError(code, code) from None
            raise


def canonical_bytes(document: dict) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def object_root() -> Path:
    from chronicle.db import configuration

    return Path(configuration().get("CHRONICLE_OBJECT_ROOT", "var/objects")).expanduser().absolute()


def object_path(sha256: str, root: Path | None = None) -> Path:
    if not isinstance(sha256, str) or not _HASH.fullmatch(sha256):
        raise ContractError("INVALID_OBJECT_HASH", "Object identity requires SHA-256.")
    base = Path(root) if root is not None else object_root()
    return base / sha256[:2] / sha256


def _directory(path: Path) -> None:
    # Persist every newly created directory entry, including the configured root;
    # fsyncing only the final hash directory would not make a new root durable.
    missing = []
    cursor = path.absolute()
    for ancestor in (cursor, *cursor.parents):
        if ancestor.is_symlink():
            raise ContractError(
                "UNSAFE_OBJECT_ROOT", "Object directories cannot be symbolic links."
            )
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        _fsync_directory(directory.parent)
    if not path.is_dir():
        raise ContractError("UNSAFE_OBJECT_ROOT", "Object root must be a directory.")
    if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
        raise ContractError("UNSAFE_OBJECT_ROOT", "Object directory must belong to the service.")
    if path.stat().st_mode & 0o022:
        raise ContractError("UNSAFE_OBJECT_ROOT", "Object directory cannot be writable by others.")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _open_object(sha256: str, byte_length: int, root: Path | None = None):
    """Open without following any directory/file symlink and validate metadata.

    Directory descriptors anchor each component, avoiding a check/open race on
    the parent prefix. Opening and fstat do not read the object's content.
    """
    path = object_path(sha256, root).absolute()
    if type(byte_length) is not int or not 0 <= byte_length <= MAX_OBJECT_BYTES:
        raise ContractError("SNAPSHOT_EVIDENCE_UNAVAILABLE", "Object size is invalid.")
    directory = None
    descriptor = None
    try:
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in path.parts[1:-1]:
            next_directory = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = next_directory
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != byte_length:
            raise ContractError(
                "SNAPSHOT_EVIDENCE_UNAVAILABLE", "Object metadata fails verification."
            )
        yield descriptor
    except OSError:
        raise ContractError(
            "SNAPSHOT_EVIDENCE_UNAVAILABLE", "Retained bytes are unavailable."
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _check_object_metadata(sha256: str, byte_length: int, root: Path | None = None) -> None:
    """Cheap request check; same-size content corruption requires explicit audit."""
    with _open_object(sha256, byte_length, root):
        pass


def _hash_object(
    sha256: str, byte_length: int, root: Path | None = None, *, output: io.BytesIO | None = None
) -> None:
    """Verify all actual bytes in bounded chunks; retain bytes only when requested."""
    with _open_object(sha256, byte_length, root) as descriptor:
        digest, actual_length = hashlib.sha256(), 0
        while chunk := os.read(descriptor, HASH_CHUNK_BYTES):
            actual_length += len(chunk)
            if actual_length > byte_length:
                raise ContractError(
                    "SNAPSHOT_EVIDENCE_UNAVAILABLE", "Retained bytes exceed their recorded size."
                )
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
        if actual_length != byte_length or digest.hexdigest() != sha256:
            raise ContractError(
                "SNAPSHOT_EVIDENCE_UNAVAILABLE", "Retained bytes fail verification."
            )


def _verified_bytes(sha256: str, byte_length: int, root: Path | None = None) -> bytes:
    with io.BytesIO() as output:
        _hash_object(sha256, byte_length, root, output=output)
        return output.getvalue()


@contextmanager
def _object_lock(conn: psycopg.Connection, sha256: str):
    # A session lock spans the committed registration and filesystem promotion.
    # Closing a failed worker connection releases it; ticket references survive.
    key = int(sha256[:16], 16)
    if key >= 2**63:
        key -= 2**64
    conn.execute("SELECT pg_advisory_lock(%s)", (key,))
    try:
        yield
    finally:
        if not conn.closed:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))


def stage_bytes(
    conn: psycopg.Connection,
    dataset_id: str,
    raw: bytes,
    rights_policy: dict,
    source_metadata: dict,
    *,
    worker_id: UUID | None = None,
    ticket_id: UUID | None = None,
    root: Path | None = None,
    _fault=None,
) -> StagedObject:
    """Register before promotion, fsync, then mark ready; exact hash never overwrites.

    `_fault` is an internal test hook with durable phase names, never an API input.
    Failed tickets stay active until their worker explicitly ends them or a
    quiesced supervisor has established that worker termination actually occurred.
    """
    if not isinstance(raw, bytes) or len(raw) > MAX_OBJECT_BYTES:
        raise ContractError("OBJECT_TOO_LARGE", "Object must be bounded bytes.")
    if not conn.autocommit or conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ContractError(
            "OBJECT_TRANSACTION_ACTIVE", "Object staging requires an idle connection."
        )
    worker, ticket = worker_id or uuid4(), ticket_id or uuid4()
    sha256 = hashlib.sha256(raw).hexdigest()
    base = Path(root) if root is not None else object_root()
    with _errors(), conn.transaction():
        policy_hash = conn.execute(
            "SELECT chronicle.open_object_ticket(%s,%s,%s,%s,%s)",
            (
                ticket,
                dataset_id,
                worker,
                canonical_bytes(rights_policy).decode(),
                Jsonb(source_metadata),
            ),
        ).fetchone()[0]
    _directory(base)
    _directory(base / "tmp")
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=f"{ticket}-", dir=base / "tmp")
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if _fault:
            _fault("temporary_durable")
        with _errors(), _object_lock(conn, sha256):
            with conn.transaction():
                conn.execute(
                    "SELECT chronicle.register_object(%s,%s,%s,%s)",
                    (ticket, worker, sha256, len(raw)),
                )
            if _fault:
                _fault("registered")
            destination = object_path(sha256, base)
            _directory(destination.parent)
            _fsync_directory(base)
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                _hash_object(sha256, len(raw), base)
            _fsync_directory(destination.parent)
            if _fault:
                _fault("promoted")
            # Verify existing/adopted content as well as newly written bytes.
            _hash_object(sha256, len(raw), base)
            with conn.transaction():
                conn.execute("SELECT chronicle.object_ready(%s,%s,%s)", (ticket, worker, sha256))
            if _fault:
                _fault("ready")
        return StagedObject(ticket, worker, sha256, policy_hash)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def end_ticket(conn: psycopg.Connection, staged: StagedObject, *, abandoned: bool = False) -> None:
    """Call only after the associated worker has finished or is known stopped."""
    with _errors(), conn.transaction():
        conn.execute(
            "SELECT chronicle.end_object_ticket(%s,%s,%s)",
            (staged.ticket_id, staged.worker_id, abandoned),
        )


def attach_batch_object(
    conn: psycopg.Connection, dataset_id: str, batch_id: str, staged: StagedObject
) -> None:
    with _errors(), conn.transaction():
        conn.execute(
            "SELECT chronicle.attach_batch_artifact(%s,%s,%s,%s,%s)",
            (dataset_id, batch_id, staged.ticket_id, staged.worker_id, staged.sha256),
        )


def publish_snapshot(
    conn: psycopg.Connection,
    dataset_id: str,
    snapshot_id: str,
    new_batch_ids,
    expected_parent: str | None,
    request_id: UUID,
    *,
    root: Path | None = None,
    _fault=None,
) -> str:
    """Durably publish the explicit parent union; exact request replay never rewinds."""
    if (
        not isinstance(new_batch_ids, (list, tuple, set, frozenset))
        or not new_batch_ids
        or any(
            not isinstance(value, str) or not value or value.strip() != value or len(value) > 200
            for value in new_batch_ids
        )
    ):
        raise ContractError("INVALID_PUBLICATION", "New validated batch IDs are required.")
    batch_ids = sorted(set(new_batch_ids), key=lambda value: value.encode())
    request = {
        "dataset_id": dataset_id,
        "snapshot_id": snapshot_id,
        "expected_parent": expected_parent,
        "new_batch_ids": batch_ids,
    }
    # A response-lost retry is possible even after later pointers have moved.
    previous = conn.execute(
        "SELECT snapshot_id,request_document FROM chronicle.artifact_publication "
        "WHERE request_id=%s",
        (request_id,),
    ).fetchone()
    if previous:
        if previous[1] != request:
            raise ContractError("PUBLICATION_IDENTITY_CONFLICT", "Publication identity differs.")
        require_snapshot_access(conn, dataset_id, previous[0], root=root)
        return previous[0]
    parent_batches = conn.execute(
        "SELECT batch_id FROM chronicle.snapshot_batch WHERE dataset_id=%s AND snapshot_id=%s",
        (dataset_id, expected_parent),
    ).fetchall()
    batches = sorted(
        set(batch_ids) | {row[0] for row in parent_batches}, key=lambda value: value.encode()
    )
    manifest = conn.execute(
        "SELECT chronicle.artifact_manifest(%s,%s,%s,%s)",
        (dataset_id, snapshot_id, expected_parent, batches),
    ).fetchone()[0]
    # All current raw object bytes must be available before the DB publication.
    for artifact in manifest["artifacts"]:
        _hash_object(artifact["object_sha256"], artifact["byte_length"], root)
    staged = stage_bytes(
        conn,
        dataset_id,
        canonical_bytes(manifest),
        _MANIFEST_RIGHTS,
        {"kind": "snapshot_manifest"},
        root=root,
    )
    try:
        if _fault:
            _fault("manifest_ready")
        with _errors(), conn.transaction():
            result = conn.execute(
                "SELECT chronicle.publish_artifact_snapshot(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    request_id,
                    dataset_id,
                    snapshot_id,
                    expected_parent,
                    batch_ids,
                    canonical_bytes(manifest).decode(),
                    staged.ticket_id,
                    staged.worker_id,
                    staged.sha256,
                ),
            ).fetchone()[0]
            if _fault:
                _fault("before_commit")
        if _fault:
            _fault("after_commit")
        return result
    except Exception:
        end_ticket(conn, staged, abandoned=True)
        raise


def require_snapshot_access(
    conn: psycopg.Connection,
    dataset_id: str,
    snapshot_id: str,
    use: str = "local_analysis",
    *,
    root: Path | None = None,
) -> None:
    """Check current rights and ready/regular/non-symlink/size metadata only.

    Full content verification belongs to actual object reads and explicit audits;
    report requests do not reread and hash the full retained corpus.
    """
    if not conn.execute(
        "SELECT 1 FROM chronicle.snapshot WHERE dataset_id=%s AND snapshot_id=%s",
        (dataset_id, snapshot_id),
    ).fetchone():
        raise ContractError("UNKNOWN_SNAPSHOT", "Snapshot does not exist.")
    policies = conn.execute(
        "SELECT p.policy,r.policy_sha256 FROM chronicle.snapshot_batch s "
        "JOIN chronicle.batch_artifact a ON a.dataset_id=s.dataset_id AND a.batch_id=s.batch_id "
        "JOIN chronicle.rights_policy p ON p.policy_sha256=a.policy_sha256 "
        "LEFT JOIN chronicle.rights_revocation r ON r.policy_sha256=p.policy_sha256 "
        "WHERE s.dataset_id=%s AND s.snapshot_id=%s",
        (dataset_id, snapshot_id),
    ).fetchall()
    if any(revoked or use not in policy.get("permitted_uses", []) for policy, revoked in policies):
        raise ContractError("RIGHTS_DENIED", "Current source rights do not permit this use.")
    for digest, length, state in conn.execute(
        "SELECT o.object_sha256,o.byte_length,o.state FROM chronicle.snapshot_object s "
        "JOIN chronicle.storage_object o USING(object_sha256) "
        "WHERE s.dataset_id=%s AND s.snapshot_id=%s",
        (dataset_id, snapshot_id),
    ).fetchall():
        if state != "ready":
            raise ContractError("SNAPSHOT_EVIDENCE_UNAVAILABLE", "Retained object is not ready.")
        _check_object_metadata(digest, length, root)


def read_object(
    conn: psycopg.Connection,
    dataset_id: str,
    snapshot_id: str,
    sha256: str,
    use: str = "local_analysis",
    *,
    root: Path | None = None,
) -> bytes:
    require_snapshot_access(conn, dataset_id, snapshot_id, use, root=root)
    obj = conn.execute(
        "SELECT o.byte_length FROM chronicle.snapshot_object s JOIN chronicle.storage_object o "
        "USING(object_sha256) WHERE s.dataset_id=%s AND s.snapshot_id=%s AND s.object_sha256=%s",
        (dataset_id, snapshot_id, sha256),
    ).fetchone()
    if obj is None:
        raise ContractError("OBJECT_NOT_IN_SNAPSHOT", "Object is outside the pinned snapshot.")
    return _verified_bytes(sha256, obj[0], root)


def verify_closure(conn: psycopg.Connection, *, root: Path | None = None) -> list[dict]:
    """Stream-hash permanent bytes; rights blocking is a separate access decision."""
    failures = []
    for digest, length, state in conn.execute(
        "SELECT DISTINCT o.object_sha256,o.byte_length,o.state FROM chronicle.snapshot_object s "
        "JOIN chronicle.storage_object o USING(object_sha256) ORDER BY o.object_sha256"
    ).fetchall():
        try:
            if state != "ready":
                raise ContractError("SNAPSHOT_EVIDENCE_UNAVAILABLE", "Object is not ready.")
            _hash_object(digest, length, root)
        except ContractError as exc:
            failures.append({"object_sha256": digest, "code": exc.code})
    return failures


def revoke_rights(conn: psycopg.Connection, policy_sha256: str, reason: str) -> None:
    with _errors(), conn.transaction():
        conn.execute("SELECT chronicle.revoke_rights(%s,%s)", (policy_sha256, reason))


def collect_abandoned(
    conn: psycopg.Connection, *, grace_seconds: int = 86400, root: Path | None = None, _fault=None
) -> list[str]:
    """Collect only registered objects with ended tickets and no permanent refs.

    Never scan/delete unknown hash files or incomplete temporary files online.
    A live ticket is preserved even when its creation time is old.
    """
    if type(grace_seconds) is not int or grace_seconds < 0:
        raise ContractError("INVALID_GRACE_PERIOD", "Grace period must be nonnegative seconds.")
    deleted = []
    candidates = conn.execute(
        "SELECT object_sha256 FROM chronicle.storage_object WHERE state <> 'deleted' "
        "ORDER BY object_sha256"
    ).fetchall()
    for (digest,) in candidates:
        with _errors(), _object_lock(conn, digest):
            with conn.transaction():
                permitted = conn.execute(
                    "SELECT chronicle.begin_object_deletion(%s,%s)", (digest, grace_seconds)
                ).fetchone()[0]
            if not permitted:
                continue
            if _fault:
                _fault("deleting")
            path = object_path(digest, root)
            if path.parent.is_symlink():
                raise ContractError("UNSAFE_OBJECT_ROOT", "Object directory is unsafe.")
            path.unlink(missing_ok=True)
            if path.parent.exists():
                _fsync_directory(path.parent)
            if _fault:
                _fault("unlinked")
            with conn.transaction():
                conn.execute("SELECT chronicle.finish_object_deletion(%s)", (digest,))
            deleted.append(digest)
    return deleted


@contextmanager
def snapshot_pin(
    conn: psycopg.Connection,
    dataset_id: str,
    snapshot_id: str,
    *,
    worker_id: UUID | None = None,
    root: Path | None = None,
):
    worker, pin = worker_id or uuid4(), uuid4()
    require_snapshot_access(conn, dataset_id, snapshot_id, root=root)
    with _errors(), conn.transaction():
        conn.execute(
            "SELECT chronicle.pin_snapshot(%s,%s,%s,%s)", (pin, dataset_id, snapshot_id, worker)
        )
    try:
        yield pin
    finally:
        with _errors(), conn.transaction():
            conn.execute("SELECT chronicle.release_snapshot_pin(%s,%s)", (pin, worker))


def read_staged_object(
    conn: psycopg.Connection,
    staged: StagedObject,
    *,
    root: Path | None = None,
    use: str = "local_ingest",
) -> bytes:
    """Read a retained acquisition by its service-owned ticket identity, with live rights."""
    row = conn.execute(
        "SELECT o.byte_length,o.state,p.policy,r.policy_sha256,t.state "
        "FROM chronicle.staging_ticket t JOIN chronicle.ticket_object x USING(ticket_id) "
        "JOIN chronicle.storage_object o USING(object_sha256) "
        "JOIN chronicle.rights_policy p ON p.policy_sha256=t.policy_sha256 "
        "LEFT JOIN chronicle.rights_revocation r ON r.policy_sha256=p.policy_sha256 "
        "WHERE t.ticket_id=%s AND t.worker_id=%s AND t.policy_sha256=%s AND o.object_sha256=%s",
        (staged.ticket_id, staged.worker_id, staged.policy_sha256, staged.sha256),
    ).fetchone()
    if row is None:
        raise ContractError("OBJECT_NOT_IN_TICKET", "Acquisition identity does not match.")
    if row[3] is not None or use not in row[2].get("permitted_uses", []):
        raise ContractError("RIGHTS_DENIED", "Current source rights do not permit this use.")
    if row[1] != "ready" or row[4] not in ("active", "closed"):
        raise ContractError("SNAPSHOT_EVIDENCE_UNAVAILABLE", "Acquisition is not available.")
    return _verified_bytes(staged.sha256, row[0], root)
