"""Bounded reproducibility bundles: retained outputs, inputs and matching code.

Checksums establish content integrity, not publisher authenticity. Import never
executes bundled code or extracts files. An absent historical environment is
explicit; retained report replay is still available without recomputation.
"""

import hashlib
import io
import json
import os
import re
import stat
import sys
import zipfile
from importlib.resources import files
from pathlib import Path, PurePosixPath
from uuid import UUID

import psycopg

from chronicle import reports
from chronicle.errors import ContractError
from chronicle.objects import require_snapshot_access

MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_FILES = 512
WHEEL_PREFIX = "environment/wheels/"
REQUIREMENTS_MEMBER = "environment/src/chronicle/runtime-requirements.lock"
OFFLINE_INSTALL = """# Offline installation

Run from this `environment/` directory with uv and the recorded Python version.
No network access or package cache is needed when `wheels/` is present:

    uv venv --python "$(cat .python-version)" .venv
    uv pip install --python .venv/bin/python --offline --no-index \\
        --find-links wheels --require-hashes -r src/chronicle/runtime-requirements.lock
    PYTHONPATH=src .venv/bin/python -c "import chronicle"

Every wheel's SHA-256 is listed in `src/chronicle/runtime-requirements.lock`, and
`--require-hashes` makes the installer check it. The wheels were collected for the
platform recorded in `manifest.json` (`dependency_wheels_platform`); another
platform needs network access or its own wheels.
"""
SOURCE_NAMES = (
    "reports.py",
    "evidence.py",
    "numeric.py",
    "domain.py",
    "store.py",
    "errors.py",
    "incremental.py",
    "sql/select_evidence_v1.sql",
    "sql/select_context.sql",
)


def _hash(payload):
    return hashlib.sha256(payload).hexdigest()


def _collect_sources(resource, prefix=""):
    result = {}
    for child in sorted(resource.iterdir(), key=lambda item: item.name):
        name = prefix + child.name
        if child.is_dir() and child.name != "__pycache__":
            result.update(_collect_sources(child, name + "/"))
        elif child.is_file() and PurePosixPath(name).suffix in (
            ".py",
            ".sql",
            ".json",
            ".lock",
            ".sha256",
            ".html",
            ".js",
            ".css",
            ".toml",
            ".md",
        ):
            result["environment/src/chronicle/" + name] = child.read_bytes()
    return result


def _requirements(text):
    """Parse an exported, hash-pinned requirements file into (name, marker, hashes)."""
    entries = []
    for block in re.split(r"\n(?=[A-Za-z0-9])", text):
        lines = [line.strip().rstrip("\\").strip() for line in block.splitlines()]
        head = lines[0] if lines else ""
        if not head or head.startswith("#"):
            continue
        requirement, _, marker = head.partition(";")
        name = re.split(r"[=<>!~ ]", requirement.strip(), maxsplit=1)[0]
        hashes = {
            match.group(1)
            for line in lines
            for match in re.finditer(r"--hash=sha256:([0-9a-f]{64})", line)
        }
        if name and hashes:
            entries.append((_normalize(name), marker.strip() or None, hashes))
    return entries


def _normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _marker_applies(marker):
    """Evaluate the simple markers uv exports; None means "cannot tell"."""
    if marker is None:
        return True
    match = re.fullmatch(r"(\w+)\s*(==|!=)\s*'([^']*)'", marker)
    values = {
        "implementation_name": sys.implementation.name,
        "sys_platform": sys.platform,
        "os_name": os.name,
    }
    if match is None or match.group(1) not in values:
        return None
    equal = values[match.group(1)] == match.group(3)
    return equal if match.group(2) == "==" else not equal


def _collect_wheels(wheelhouse, requirements_text):
    """Return ({member: bytes}, limitation). All required wheels, or none at all.

    Only wheels whose SHA-256 appears in the hash-pinned requirements are taken.
    A requirement whose marker applies here must have one; otherwise the bundle
    carries no wheels and says why, rather than shipping a partial set.
    """
    if not wheelhouse:
        return {}, "No wheelhouse configured (CHRONICLE_WHEELHOUSE); run make wheelhouse"
    directory = Path(wheelhouse)
    if not directory.is_dir():
        return {}, "Configured wheelhouse directory does not exist"
    requirements = _requirements(requirements_text)
    allowed = {digest for _, _, hashes in requirements for digest in hashes}
    found = {}
    for path in sorted(directory.glob("*.whl")):
        data = path.read_bytes()
        digest = _hash(data)
        if digest in allowed:
            found[digest] = (path.name, data)
    missing = [
        name
        for name, marker, hashes in requirements
        if _marker_applies(marker) is True and not hashes & set(found)
    ]
    if missing:
        return {}, "Dependency wheels missing for: " + ", ".join(sorted(missing))
    return {WHEEL_PREFIX + name: data for name, data in found.values()}, None


def export_report_bundle(conn: psycopg.Connection, report_id: UUID) -> bytes:
    """Build an archive only when current source policies permit public export."""
    report = reports.replay_report(conn, report_id)
    require_snapshot_access(conn, report["dataset_id"], report["snapshot_id"], "public_export")
    contents = {"report.json": reports.canonical_json(report)}
    inputs = {
        item["observation_month"]: {key: value for key, value in item.items() if key != "display"}
        for row in report["rows"]
        for item in row["inputs"]
    }
    contents["inputs.json"] = reports.canonical_json([inputs[key] for key in sorted(inputs)])
    current = reports.implementation_identity()
    environment_available = current == report["implementation"]
    reason = None if environment_available else "Recorded source/runtime identity is not active"
    if environment_available:
        package = files("chronicle")
        try:
            lock = package.joinpath("runtime.lock").read_bytes()
        except FileNotFoundError:
            lock = b""
        if _hash(lock) != current["dependency_lock_sha256"]:
            environment_available = False
            reason = "Exact dependency lock is not packaged"
        else:
            contents.update(_collect_sources(package))
            contents["environment/uv.lock"] = lock
            contents["environment/pyproject.toml"] = package.joinpath(
                "runtime-pyproject.toml"
            ).read_bytes()
            contents["environment/README.md"] = package.joinpath("runtime-README.md").read_bytes()
            contents["environment/.python-version"] = (current["python"] + "\n").encode()
            contents["environment/runtime.json"] = reports.canonical_json(current)
            wheels, wheel_limitation = _collect_wheels(
                _configured_wheelhouse(), contents[REQUIREMENTS_MEMBER].decode()
            )
            contents.update(wheels)
            if wheels:
                contents["environment/OFFLINE-INSTALL.md"] = OFFLINE_INSTALL.encode()
    if not environment_available:
        wheels, wheel_limitation = {}, "Environment is not included"
    manifest = {
        "schema": "chronicle.reproducibility-bundle.v1",
        "report_id": str(report_id),
        "environment_available": environment_available,
        "environment_limitation": reason,
        "dependency_wheels": "included" if wheels else "not_included",
        "dependency_wheels_limitation": wheel_limitation,
        "dependency_wheels_platform": (
            {"sys_platform": sys.platform, "python": current["python"]} if wheels else None
        ),
        "identity_replay": True,
        "execution_performed": False,
        "files": {
            name: {"sha256": _hash(data), "size": len(data)}
            for name, data in sorted(contents.items())
        },
    }
    contents["manifest.json"] = reports.canonical_json(manifest)
    if len(contents) > MAX_BUNDLE_FILES or sum(map(len, contents.values())) > MAX_BUNDLE_BYTES:
        raise ContractError(
            "BUNDLE_TOO_LARGE", "Reproducibility archive exceeds its resource limit."
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return buffer.getvalue()


def verify_bundle(payload: bytes) -> dict:
    """Read only fixed bounded members; reject traversal, duplicates and bad hashes."""
    if not isinstance(payload, bytes) or len(payload) > MAX_BUNDLE_BYTES:
        raise ContractError("INVALID_BUNDLE", "Bundle must be bounded archive bytes.")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > MAX_BUNDLE_FILES or len(set(names)) != len(names):
                raise ValueError("Member count or duplicate member")
            if sum(entry.file_size for entry in entries) > MAX_BUNDLE_BYTES:
                raise ValueError("Expanded content bound")
            contents = {}
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in name
                    or str(path) != name
                    or entry.is_dir()
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise ValueError("Unsafe member name or type")
                if name not in ("manifest.json", "report.json", "inputs.json") and not (
                    name.startswith("environment/")
                ):
                    raise ValueError("Unsupported member")
                contents[name] = archive.read(entry)
            manifest = json.loads(contents.pop("manifest.json"))
            if manifest["schema"] != "chronicle.reproducibility-bundle.v1":
                raise ValueError("Unsupported schema")
            if set(contents) != set(manifest["files"]):
                raise ValueError("Incomplete manifest")
            for name, data in contents.items():
                if manifest["files"][name] != {"sha256": _hash(data), "size": len(data)}:
                    raise ValueError("Content digest mismatch")
            report = json.loads(contents["report.json"])
            inputs = json.loads(contents["inputs.json"])
            if report["report_id"] != manifest["report_id"]:
                raise ValueError("Report identity mismatch")
            expected_inputs = {
                item["observation_month"]: {
                    key: value for key, value in item.items() if key != "display"
                }
                for row in report["rows"]
                for item in row["inputs"]
            }
            if inputs != [expected_inputs[key] for key in sorted(expected_inputs)]:
                raise ValueError("Input closure mismatch")
            if manifest["environment_available"]:
                identity = report["implementation"]
                if _hash(contents["environment/uv.lock"]) != identity["dependency_lock_sha256"]:
                    raise ValueError("Dependency lock mismatch")
                if _hash(contents["environment/pyproject.toml"]) != identity["project_sha256"]:
                    raise ValueError("Project declaration mismatch")
                source_hashes = {
                    name: _hash(contents["environment/src/chronicle/" + name])
                    for name in SOURCE_NAMES
                }
                if _hash(reports.canonical_json(source_hashes)) != identity["source_sha256"]:
                    raise ValueError("Recorded code mismatch")
                if json.loads(contents["environment/runtime.json"]) != identity:
                    raise ValueError("Recorded runtime mismatch")
            wheel_members = [name for name in contents if name.startswith(WHEEL_PREFIX)]
            if wheel_members:
                if manifest.get("dependency_wheels") != "included":
                    raise ValueError("Undeclared dependency wheels")
                allowed = {
                    digest
                    for _, _, hashes in _requirements(contents[REQUIREMENTS_MEMBER].decode())
                    for digest in hashes
                }
                for name in wheel_members:
                    if not name.endswith(".whl") or "/" in name[len(WHEEL_PREFIX) :]:
                        raise ValueError("Unexpected wheel member")
                    if _hash(contents[name]) not in allowed:
                        raise ValueError("Wheel is not in the hash-pinned requirements")
            elif manifest.get("dependency_wheels") == "included":
                raise ValueError("Declared dependency wheels are missing")
            return {
                "status": "verified",
                "manifest": manifest,
                "report": report,
                "integrity_only": True,
                "execution_performed": False,
            }
    except (
        ValueError,
        KeyError,
        TypeError,
        UnicodeError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        RecursionError,
        NotImplementedError,
    ):
        raise ContractError(
            "INVALID_BUNDLE", "Bundle content or manifest failed validation."
        ) from None


def _configured_wheelhouse():
    from chronicle import db

    return os.environ.get("CHRONICLE_WHEELHOUSE") or db.configuration().get("CHRONICLE_WHEELHOUSE")
