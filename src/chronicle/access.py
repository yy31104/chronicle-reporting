"""Explicit local service identities, reloaded on every request for revocation."""

import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chronicle.errors import ContractError


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    datasets: frozenset[str]

    def require(self, dataset_id: str, *, operator: bool = False) -> None:
        if dataset_id not in self.datasets or (operator and self.role != "operator"):
            raise ContractError("ACCESS_DENIED", "Access denied")


def load_policy(path: Path) -> dict:
    policy = json.loads(path.read_text())
    if (
        not isinstance(policy, dict)
        or set(policy) != {"schema", "identities"}
        or policy["schema"] != "chronicle.access.v1"
    ):
        raise ValueError("Invalid access policy")
    entries = policy["identities"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ValueError("Invalid identities")
    names, tokens = set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "name",
            "role",
            "datasets",
            "enabled",
            "token_sha256",
        }:
            raise ValueError("Invalid identity")
        name, token = entry["name"], entry["token_sha256"]
        if not isinstance(name, str) or not name or len(name) > 100 or name in names:
            raise ValueError("Invalid identity name")
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[0-9a-f]{64}", token)
            or token in tokens
        ):
            raise ValueError("Invalid identity token")
        if entry["role"] not in {"viewer", "operator"} or type(entry["enabled"]) is not bool:
            raise ValueError("Invalid identity role/state")
        datasets = entry["datasets"]
        if (
            not isinstance(datasets, list)
            or len(datasets) > 1000
            or any(
                not isinstance(d, str) or not d or len(d) > 200 or d.strip() != d for d in datasets
            )
            or len(set(datasets)) != len(datasets)
        ):
            raise ValueError("Invalid dataset scope")
        names.add(name)
        tokens.add(token)
    return policy


def authenticate(token: str, path: Path) -> Principal:
    # Hashes permit rotation/revocation without storing live bearer secrets here.
    # A malformed or missing configuration fails closed, including on startup.
    payload = load_policy(path)
    digest = hashlib.sha256(token.encode()).hexdigest()
    for entry in payload["identities"]:
        if secrets.compare_digest(digest, entry["token_sha256"]):
            if entry["role"] not in ("viewer", "operator") or not entry["enabled"]:
                break
            return Principal(entry["name"], entry["role"], frozenset(entry["datasets"]))
    raise ContractError("UNAUTHENTICATED", "Authentication required")


def configure_access(directory: Path, datasets: list[str]) -> tuple[Path, Path]:
    """Create once; never overwrite existing identities or print bearer tokens."""
    if not datasets or any(not value or len(value) > 200 for value in datasets):
        raise ContractError("INVALID_IDENTITY", "Explicit dataset IDs are required")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    policy_directory = directory / "policy"
    policy_directory.mkdir(mode=0o700, exist_ok=True)
    policy = policy_directory / "access.json"
    credentials = directory / "credentials.json"
    if policy.exists() or credentials.exists():
        raise ContractError("ACCESS_CONFIG_EXISTS", "Existing access configuration was preserved")
    identities = []
    tokens = {}
    for role in ("viewer", "operator"):
        token = secrets.token_urlsafe(48)
        tokens[role] = token
        identities.append(
            {
                "name": f"local-{role}",
                "role": role,
                "datasets": datasets,
                "enabled": True,
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            }
        )
    for path, value in (
        (credentials, tokens),
        (policy, {"schema": "chronicle.access.v1", "identities": identities}),
    ):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
    return policy, credentials


def _replace_private_json(path: Path, value: dict) -> None:
    """Publish a durable private file within its directory (including bind mounts)."""
    descriptor, temporary = tempfile.mkstemp(prefix=".access-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def rotate_access(directory: Path) -> tuple[Path, Path]:
    """Rotate both local tokens, preserving every scope and enabled state.

    Credentials are durable before the policy activates them. If interrupted between
    the two replacements, rerun rotation; no partial policy is ever published.
    A process lock serializes concurrent rotations. No token is returned or logged.
    """
    policy = directory / "policy" / "access.json"
    credentials = directory / "credentials.json"
    if (
        directory.is_symlink()
        or policy.parent.is_symlink()
        or any(path.is_symlink() for path in (policy, credentials))
    ):
        raise ContractError("INVALID_ACCESS_PATH", "Access configuration cannot use symlinks")
    lock = directory / ".access-rotate.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        payload = load_policy(policy)
        if {entry["name"] for entry in payload["identities"]} != {
            "local-viewer",
            "local-operator",
        } or any(entry["name"] != f"local-{entry['role']}" for entry in payload["identities"]):
            raise ContractError("INVALID_IDENTITY", "Rotation requires the two local identities")
        tokens = {role: secrets.token_urlsafe(48) for role in ("viewer", "operator")}
        for entry in payload["identities"]:
            entry["token_sha256"] = hashlib.sha256(tokens[entry["role"]].encode()).hexdigest()
        _replace_private_json(credentials, tokens)
        _replace_private_json(policy, payload)
    return policy, credentials
