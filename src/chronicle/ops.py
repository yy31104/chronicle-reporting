"""Local PostgreSQL 18 backup and isolated restore rehearsal with object closure.

The Docker Compose database service is the explicitly supported local topology.
Backups include no passwords. Restore accepts only a Chronicle-produced manifest,
checks all hashes before touching PostgreSQL, and creates a fresh isolated database.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from psycopg import sql

from chronicle import db
from chronicle.errors import ContractError


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _docker(command, *, stdin=None, stdout=None):
    container = db.configuration().get("CHRONICLE_DB_CONTAINER", "chronicle-postgres-1")
    result = subprocess.run(
        ["docker", "exec", "-i", container, *command],
        stdin=stdin,
        stdout=stdout or subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    if result.returncode:
        raise ContractError("BACKUP_TOOL_FAILED", "Local PostgreSQL backup tool failed")
    if result.stderr:
        # Do not silently call a warning-bearing backup verified.
        raise ContractError("BACKUP_TOOL_WARNING", "Local backup tool emitted diagnostics")


def backup(destination: Path, *, database=None, root=None):
    from chronicle.objects import object_path

    destination = destination.resolve()
    if destination.exists():
        raise ContractError("BACKUP_EXISTS", "Backups are never overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / (".chronicle-backup-" + uuid4().hex)
    staging.mkdir(mode=0o700)
    started = time.monotonic()
    try:
        with db.connect("admin", database=database) as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            name = conn.info.dbname
            version = conn.execute("SHOW server_version").fetchone()[0]
            published = conn.execute(
                "SELECT DISTINCT object_sha256 FROM chronicle.snapshot_object "
                "ORDER BY object_sha256"
            ).fetchall()
            refs = conn.execute(
                "SELECT object_sha256 FROM chronicle.snapshot_object UNION "
                "SELECT r.object_sha256 FROM chronicle.ticket_object r "
                "JOIN chronicle.staging_ticket t USING(ticket_id) "
                "JOIN chronicle.storage_object o USING(object_sha256) "
                "WHERE t.state='active' AND o.state='ready' ORDER BY object_sha256"
            ).fetchall()
            # Ended-ticket collection may otherwise race a snapshot-consistent
            # dump of an active acquisition. Hold hash locks through durable copy.
            for (digest,) in refs:
                key = int(digest[:16], 16)
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (key - 2**64 if key >= 2**63 else key,)
                )
            reports = conn.execute(
                "SELECT report_id,document_sha256 FROM chronicle.report_run ORDER BY report_id"
            ).fetchall()
            migrations = conn.execute(
                "SELECT name,sha256 FROM chronicle.schema_migration ORDER BY name"
            ).fetchall()
            with (staging / "database.dump").open("xb") as output:
                _docker(
                    [
                        "pg_dump",
                        "-U",
                        "postgres",
                        "--format=custom",
                        "--snapshot",
                        snapshot,
                        "--dbname",
                        name,
                    ],
                    stdout=output,
                )
                output.flush()
                os.fsync(output.fileno())
            hashes = {"database.dump": _hash(staging / "database.dump")}
            for (digest,) in refs:
                origin = object_path(digest, root=root)
                if not origin.is_file() or origin.is_symlink() or _hash(origin) != digest:
                    raise ContractError("OBJECT_CORRUPT", "Referenced object is missing or corrupt")
                relative = f"objects/{digest[:2]}/{digest}"
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, target)
                if _hash(target) != digest:
                    raise ContractError("OBJECT_CORRUPT", "Copied object checksum differs")
                _sync(target)
                _sync(target.parent)
                hashes[relative] = digest
        manifest = {
            "backup_schema": "chronicle.backup.v1",
            "created_at": datetime.now(UTC).isoformat(),
            "database": name,
            "server_version": version,
            "files": hashes,
            "migrations": migrations,
            "reports": [[str(key), digest] for key, digest in reports],
            "published_object_count": len(published),
            "retained_object_count": len(refs),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        _sync(staging / "manifest.json")
        if (staging / "objects").exists():
            _sync(staging / "objects")
        _sync(staging)
        os.rename(staging, destination)
        _sync(destination.parent)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_backup(directory: Path):
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("backup_schema") != "chronicle.backup.v1":
        raise ContractError("INVALID_BACKUP", "Unknown backup schema")
    files = manifest.get("files", {})
    if "database.dump" not in files or len(files) > 100000:
        raise ContractError("INVALID_BACKUP", "Backup file list is invalid")
    for name, digest in files.items():
        if not (name == "database.dump" or re.fullmatch(r"objects/[0-9a-f]{2}/[0-9a-f]{64}", name)):
            raise ContractError("INVALID_BACKUP", "Backup path is not allowed")
        path = directory / name
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ContractError("INVALID_BACKUP", "Backup path escapes its directory")
        if not path.is_file() or _hash(path) != digest:
            raise ContractError("BACKUP_CORRUPT", "Backup file checksum failed")
    return manifest


def _restore_verified(directory, name, root, manifest):
    from chronicle.objects import verify_closure

    with (directory / "database.dump").open("rb") as source:
        _docker(
            [
                "pg_restore",
                "-U",
                "postgres",
                "--exit-on-error",
                "--single-transaction",
                "--dbname",
                name,
            ],
            stdin=source,
        )
    with db.connect("admin", database=name) as conn:
        conn.execute(
            sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
        )
        issues = verify_closure(conn, root=root)
        if issues:
            raise ContractError("RESTORE_CLOSURE_FAILED", "Restored object closure is incomplete")
        reports = conn.execute(
            "SELECT report_id,document_sha256 FROM chronicle.report_run ORDER BY report_id"
        ).fetchall()
        if [[str(key), digest] for key, digest in reports] != manifest["reports"]:
            raise ContractError("RESTORE_REPORT_MISMATCH", "Restored report manifest differs")
        migrations = conn.execute(
            "SELECT name,sha256 FROM chronicle.schema_migration ORDER BY name"
        ).fetchall()
        if [list(row) for row in migrations] != manifest["migrations"]:
            raise ContractError("RESTORE_MIGRATION_MISMATCH", "Migration history differs")
        from chronicle.reports import replay_report

        for report_id, _ in reports:
            replay_report(conn, report_id)
    return len(reports)


def restore(directory: Path, *, into: str, object_root: Path):
    """Restore into a fresh retained database and object root, without switching services.

    The existing local cluster already owns the runtime roles; their passwords and
    all access tokens are provisioned separately and are never included in backups.
    Failure removes only the new resources created by this invocation.
    """
    if not re.fullmatch(r"chronicle_[a-z0-9_]{1,48}", into):
        raise ContractError("INVALID_RESTORE_TARGET", "Use a fresh chronicle_ database name")
    manifest = verify_backup(directory)
    object_root = object_root.absolute()
    if object_root.exists() or object_root.is_symlink():
        raise ContractError("RESTORE_TARGET_EXISTS", "Object destination must be new")
    if not object_root.parent.is_dir():
        raise ContractError("INVALID_RESTORE_TARGET", "Object destination parent must exist")
    started = time.monotonic()
    with db.connect("admin") as admin:
        if admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (into,)).fetchone():
            raise ContractError("RESTORE_TARGET_EXISTS", "Database destination must be new")
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER chronicle_owner").format(sql.Identifier(into))
        )
    created_root = False
    try:
        object_root.mkdir(mode=0o700)
        created_root = True
        for relative, digest in manifest["files"].items():
            if not relative.startswith("objects/"):
                continue
            target = object_root / relative.removeprefix("objects/")
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(directory / relative, target)
            if _hash(target) != digest:
                raise ContractError("OBJECT_CORRUPT", "Restored object checksum differs")
            _sync(target)
            _sync(target.parent)
        _sync(object_root)
        _sync(object_root.parent)
        count = _restore_verified(directory, into, object_root, manifest)
        return {
            "status": "verified",
            "database": into,
            "object_root": str(object_root),
            "configuration": {"CHRONICLE_DB_NAME": into, "CHRONICLE_OBJECT_ROOT": str(object_root)},
            "reports_verified": count,
            "objects_verified": manifest["retained_object_count"],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except BaseException:
        with db.connect("admin") as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(into)))
        if created_root:
            shutil.rmtree(object_root)
        raise


def restore_rehearsal(directory: Path, *, keep=False):
    """Rehearse in a new DB using verified backup objects, then discard by default."""
    manifest = verify_backup(directory)
    name = "chronicle_restore_" + uuid4().hex
    started = time.monotonic()
    with db.connect("admin") as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER chronicle_owner").format(sql.Identifier(name))
        )
    succeeded = False
    try:
        count = _restore_verified(directory, name, directory / "objects", manifest)
        succeeded = True
        return {
            "status": "verified",
            "database": name,
            "kept": keep,
            "reports_verified": count,
            "objects_verified": manifest["retained_object_count"],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    finally:
        if not keep or not succeeded:
            with db.connect("admin") as admin:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
