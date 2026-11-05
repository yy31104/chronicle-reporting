"""One-shot local daily backup job; a scheduler invokes this module, never a daemon.

Only completed, hash-verified backups created by this runner are retention candidates.
Credentials are read from runtime-root/backup.env, independently of the source checkout.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from chronicle.access import _replace_private_json
from chronicle.errors import ContractError, public_error
from chronicle.ops import backup, verify_backup

_NAME = re.compile(r"daily-\d{8}T\d{6}Z-[0-9a-f]{32}")
_MARKER = "scheduled-complete.json"
_ENV_KEYS = {
    "CHRONICLE_DB_ADMIN_PASSWORD",
    "CHRONICLE_DB_HOST",
    "CHRONICLE_DB_PORT",
    "CHRONICLE_DB_NAME",
    "CHRONICLE_DB_CONTAINER",
}


def _private_directory(path: Path) -> None:
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ContractError("INVALID_BACKUP_PATH", "Backup directories cannot use symlinks")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ContractError("INSECURE_BACKUP_PATH", "Use an owner-only backup directory")


def load_environment(root: Path) -> dict[str, str]:
    descriptor = os.open(root / "backup.env", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 65536
        ):
            raise ContractError("INSECURE_BACKUP_CONFIG", "Use an owner-only regular config file")
        content = handle.read(65537)
    result = {
        "CHRONICLE_DB_HOST": "127.0.0.1",
        "CHRONICLE_DB_PORT": "55473",
        "CHRONICLE_DB_NAME": "chronicle",
        "CHRONICLE_DB_CONTAINER": "chronicle-postgres-1",
    }
    seen = set()
    for line in content.splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in _ENV_KEYS or key in seen or not value.strip():
            raise ContractError("INVALID_BACKUP_CONFIG", "Backup configuration is invalid")
        seen.add(key)
        result[key] = value
    if "CHRONICLE_DB_ADMIN_PASSWORD" not in result:
        raise ContractError("INVALID_BACKUP_CONFIG", "Backup administrator credential is required")
    if result["CHRONICLE_DB_HOST"] not in {"127.0.0.1", "localhost", "::1"}:
        raise ContractError("INVALID_BACKUP_CONFIG", "Scheduled backups require the local database")
    if (
        not result["CHRONICLE_DB_PORT"].isdigit()
        or not 1 <= int(result["CHRONICLE_DB_PORT"]) <= 65535
    ):
        raise ContractError("INVALID_BACKUP_CONFIG", "Invalid local database port")
    return result


def _completed(path: Path, database: str) -> dict | None:
    """Do not follow or delete unknown, partial, altered or unmarked directory trees."""
    if not _NAME.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
        return None
    marker_path, manifest_path = path / _MARKER, path / "manifest.json"
    if any(p.is_symlink() or not p.is_file() for p in (marker_path, manifest_path)):
        return None
    if marker_path.stat().st_size > 4096 or manifest_path.stat().st_size > 16 * 1024 * 1024:
        return None
    try:
        marker = json.loads(marker_path.read_text())
        if (
            not isinstance(marker, dict)
            or set(marker) != {"schema", "name", "local_date", "manifest_sha256"}
            or marker["schema"] != "chronicle.scheduled-backup.v1"
            or marker["name"] != path.name
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", marker["local_date"])
            or marker["manifest_sha256"] != hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        ):
            return None
        manifest = verify_backup(path)
        if manifest.get("database") != database:
            return None
        expected = {*manifest["files"], "manifest.json", _MARKER}
        actual = set()
        for entry in path.rglob("*"):
            if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
                return None
            if entry.is_file():
                actual.add(entry.relative_to(path).as_posix())
        if actual != expected:
            return None
        return marker
    except ContractError:
        return None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def run_daily(
    root: Path, *, keep: int = 14, database: str = "chronicle", now: datetime | None = None
) -> dict:
    if type(keep) is not int or not 1 <= keep <= 365:
        raise ContractError("INVALID_BACKUP_RETENTION", "Retain between 1 and 365 daily backups")
    root = root.absolute()
    _private_directory(root)
    directory = root / "backups" / "scheduled"
    _private_directory(directory)
    state = root / "backup-status.json"
    lock_fd = os.open(root / ".backup-run.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running"}
        instant = (now or datetime.now().astimezone()).astimezone()
        today = instant.date().isoformat()
        result = {
            "schema": "chronicle.backup-status.v1",
            "attempted_at": instant.isoformat(),
            "local_date": today,
            "keep": keep,
        }
        try:
            candidates = []
            ignored = 0
            for entry in directory.iterdir():
                marker = _completed(entry, database)
                # Clock skew must not suppress today's backup or delete a copy
                # whose recorded local day is still in the future.
                if marker and marker["local_date"] <= today:
                    candidates.append((marker["local_date"], entry.name, entry))
                else:
                    ignored += 1
            candidates.sort()
            if not candidates or candidates[-1][0] < today:
                name = f"daily-{instant.astimezone(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex}"
                target = directory / name
                backup(target, root=root / "objects", database=database)
                verify_backup(target)
                _replace_private_json(
                    target / _MARKER,
                    {
                        "schema": "chronicle.scheduled-backup.v1",
                        "name": name,
                        "local_date": today,
                        "manifest_sha256": hashlib.sha256(
                            (target / "manifest.json").read_bytes()
                        ).hexdigest(),
                    },
                )
                candidates.append((today, name, target))
                candidates.sort()
                result["status"] = "completed"
            else:
                result["status"] = "already_backed_up"
            removed = []
            for _, name, path in candidates[:-keep]:
                # Recheck immediately before deletion. Unknown data is preserved.
                if _completed(path, database) is None:
                    raise ContractError("BACKUP_RETENTION_CHANGED", "Retention candidate changed")
                shutil.rmtree(path)
                removed.append(name)
            if removed:
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            result.update(
                backup=str(candidates[-1][2]),
                retained=min(len(candidates), keep),
                pruned=removed,
                preserved_unmanaged_entries=ignored,
            )
            _replace_private_json(state, result)
            return result
        except Exception as exc:
            result.update(status="failed", error=public_error(exc))
            _replace_private_json(state, result)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--keep", default=14, type=int)
    args = parser.parse_args()
    root_ready = False
    try:
        root = args.runtime_root.absolute()
        _private_directory(root)
        root_ready = True
        environment = load_environment(root)
        # Do not read the checkout .env, inherit a different DB target, or expose
        # importer/publisher/reader credentials to the backup process.
        for key in tuple(os.environ):
            if key.startswith("CHRONICLE_"):
                del os.environ[key]
        os.environ.update(environment)
        os.chdir(root)
        if shutil.which("docker") is None:
            raise ContractError("BACKUP_DOCKER_UNAVAILABLE", "Docker CLI is not on PATH")
        result = run_daily(root, keep=args.keep, database=environment["CHRONICLE_DB_NAME"])
    except Exception as exc:
        failure = {
            "schema": "chronicle.backup-status.v1",
            "status": "failed",
            "attempted_at": datetime.now().astimezone().isoformat(),
            "error": public_error(exc),
        }
        if root_ready:
            try:
                _replace_private_json(root / "backup-status.json", failure)
            except OSError:
                pass
        print(json.dumps(failure))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
