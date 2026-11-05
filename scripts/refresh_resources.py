"""Refresh byte inventories without deriving or changing expected numeric answers."""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def expected_files() -> dict[Path, bytes]:
    package = ROOT / "src/chronicle"
    lock = (ROOT / "uv.lock").read_bytes()
    result = {
        package / "runtime.lock": lock,
        package / "runtime-lock.sha256": (hashlib.sha256(lock).hexdigest() + "\n").encode(),
        package / "runtime-pyproject.toml": (ROOT / "pyproject.toml").read_bytes(),
        package / "runtime-requirements.lock": (ROOT / "requirements.lock").read_bytes(),
    }
    for checksums in sorted((ROOT / "tests/fixtures").rglob("*SHA256SUMS")):
        rows = []
        for line in checksums.read_text().splitlines():
            _, name = line.split(maxsplit=1)
            digest = hashlib.sha256((checksums.parent / name).read_bytes()).hexdigest()
            rows.append(f"{digest}  {name}\n")
        result[checksums] = "".join(rows).encode()
    rights = package / "resources/sources-rights-v1.json"
    policies = json.loads(rights.read_text())["policies"]
    policy_hashes = {
        name: hashlib.sha256(
            json.dumps(
                policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode()
        ).hexdigest()
        for name, policy in policies.items()
    }
    inventory = {
        "schema": "chronicle.resource-inventory.v1",
        "source_rights_sha256": hashlib.sha256(rights.read_bytes()).hexdigest(),
        "policy_sha256": policy_hashes,
        "archive_cells_sha256": hashlib.sha256(
            (ROOT / "data/archive/releases.json").read_bytes()
        ).hexdigest(),
    }
    result[ROOT / "data/resource-manifest.json"] = (
        json.dumps(inventory, sort_keys=True, indent=2) + "\n"
    ).encode()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for path, contents in expected_files().items():
        if args.check:
            if not path.exists() or path.read_bytes() != contents:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.write_bytes(contents)
    if stale:
        print("Stale resources: " + ", ".join(stale))
        return 1
    print("Resource inventory is current" if args.check else "Updated resource inventory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
