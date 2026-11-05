"""Archive input bounds and traversal rejection are independent of PostgreSQL."""

import hashlib
import io
import json
import stat
import warnings
import zipfile
from pathlib import Path

import pytest

from chronicle import bundles
from chronicle.errors import ContractError


def archive(items):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output, warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        for name, content in items:
            output.writestr(name, content)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "name",
    [
        "../report.json",
        "/tmp/report.json",
        "environment/../report.json",
        "environment\\report.json",
        "./report.json",
        "unapproved.json",
    ],
)
def test_bundle_rejects_unsafe_or_unsupported_paths(name):
    with pytest.raises(ContractError) as failure:
        bundles.verify_bundle(archive([(name, b"{}")]))
    assert failure.value.code == "INVALID_BUNDLE"


def test_bundle_rejects_duplicate_and_symlink_members():
    link = zipfile.ZipInfo("report.json")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    for contents in ([("report.json", b"{}"), ("report.json", b"{}")], [(link, b"../../secret")]):
        with pytest.raises(ContractError):
            bundles.verify_bundle(archive(contents))


def test_bundle_rejects_expansion_beyond_limit_before_reading(monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("report.json", b"a" * 10_000)
    data = buffer.getvalue()
    assert len(data) < 512
    monkeypatch.setattr(bundles, "MAX_BUNDLE_BYTES", 512)
    with pytest.raises(ContractError):
        bundles.verify_bundle(data)


def test_invalid_zip_and_incomplete_manifest_are_typed():
    invalid = [b"not a zip", archive([("manifest.json", json.dumps({"schema": "unexpected"}))])]
    for data in invalid:
        with pytest.raises(ContractError) as failure:
            bundles.verify_bundle(data)
        assert failure.value.code == "INVALID_BUNDLE"


def test_bundle_rejects_member_count_bound_before_parsing(monkeypatch):
    monkeypatch.setattr(bundles, "MAX_BUNDLE_FILES", 1)
    with pytest.raises(ContractError):
        bundles.verify_bundle(archive([("manifest.json", b"{}"), ("report.json", b"{}")]))


def _fake_requirements(entries):
    """entries: [(requirement head, [payload bytes])] -> hash-pinned requirements text."""
    lines = []
    for head, payloads in entries:
        lines.append(head + " \\")
        hashes = [hashlib.sha256(p).hexdigest() for p in payloads]
        for index, digest in enumerate(hashes):
            suffix = " \\" if index < len(hashes) - 1 else ""
            lines.append(f"    --hash=sha256:{digest}{suffix}")
    return "\n".join(lines) + "\n"


def test_wheels_are_collected_only_when_every_applicable_requirement_has_one(tmp_path):
    alpha, beta, windows_only = b"alpha-wheel", b"beta-wheel", b"tzdata-wheel"
    text = _fake_requirements(
        [
            ("alpha==1.0", [alpha]),
            ("beta-pkg==2.0 ; implementation_name != 'pypy'", [beta]),
            ("tzdata==1 ; sys_platform == 'win32'", [windows_only]),
        ]
    )
    (tmp_path / "alpha-1.0-py3-none-any.whl").write_bytes(alpha)
    (tmp_path / "unrelated-9-py3-none-any.whl").write_bytes(b"not locked")

    wheels, limitation = bundles._collect_wheels(tmp_path, text)
    assert wheels == {}
    assert limitation == "Dependency wheels missing for: beta-pkg"

    (tmp_path / "beta_pkg-2.0-py3-none-any.whl").write_bytes(beta)
    wheels, limitation = bundles._collect_wheels(tmp_path, text)
    assert limitation is None
    assert set(wheels) == {
        "environment/wheels/alpha-1.0-py3-none-any.whl",
        "environment/wheels/beta_pkg-2.0-py3-none-any.whl",
    }


def test_wheel_with_a_different_hash_is_not_collected(tmp_path):
    text = _fake_requirements([("alpha==1.0", [b"locked bytes"])])
    (tmp_path / "alpha-1.0-py3-none-any.whl").write_bytes(b"tampered bytes")

    wheels, limitation = bundles._collect_wheels(tmp_path, text)

    assert wheels == {}
    assert limitation == "Dependency wheels missing for: alpha"


def test_missing_wheelhouse_is_reported_not_guessed(tmp_path):
    assert bundles._collect_wheels(None, "")[1].startswith("No wheelhouse configured")
    assert bundles._collect_wheels(tmp_path / "absent", "")[1] == (
        "Configured wheelhouse directory does not exist"
    )


def test_packaged_requirements_match_the_exported_lock():
    root = Path(__file__).resolve().parents[1]
    assert (root / "src/chronicle/runtime-requirements.lock").read_bytes() == (
        root / "requirements.lock"
    ).read_bytes()
    names = {name for name, _, _ in bundles._requirements((root / "requirements.lock").read_text())}
    assert {"psycopg", "psycopg-binary", "fastapi", "uvicorn"} <= names
