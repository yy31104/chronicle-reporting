"""Run the production synthetic report flow in a disposable PostgreSQL container."""

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from chronicle import db, demo
from chronicle.errors import public_error

# Same PostgreSQL release and image digest as compose.yaml.
POSTGRES_IMAGE = (
    "postgres:18@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722"
)


class DemoError(Exception):
    """Safe, actionable diagnostics without driver messages or configuration values."""


def docker(*arguments, timeout=180):
    try:
        return subprocess.run(
            ["docker", *arguments], capture_output=True, text=True, check=True, timeout=timeout
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise DemoError(
            f"Docker {arguments[0]} failed. Check that Docker is running and retry make demo."
        ) from None


def run():
    if shutil.which("docker") is None:
        raise DemoError("Install Docker and start its engine, then retry make demo.")
    docker("info", "--format", "{{.ServerVersion}}", timeout=10)
    container = f"chronicle-demo-{uuid4().hex}"
    # Ignore checkout and inherited service configuration, including external env files.
    for key in tuple(os.environ):
        if key.startswith("CHRONICLE_"):
            del os.environ[key]
    for role in ("ADMIN", "IMPORTER", "PUBLISHER", "READER"):
        os.environ[f"CHRONICLE_DB_{role}_PASSWORD"] = secrets.token_urlsafe(32)
    os.environ.update(
        CHRONICLE_DB_HOST="127.0.0.1",
        CHRONICLE_DB_NAME="chronicle",
        POSTGRES_PASSWORD=os.environ["CHRONICLE_DB_ADMIN_PASSWORD"],
    )
    with tempfile.TemporaryDirectory(prefix="chronicle-demo-") as directory:
        previous = Path.cwd()
        os.chdir(directory)
        try:
            print(
                "Starting disposable PostgreSQL 18 (first run may download the image)...",
                flush=True,
            )
            docker(
                "run",
                "--detach",
                "--name",
                container,
                "--publish",
                "127.0.0.1::5432",
                "--env",
                "POSTGRES_PASSWORD",
                "--env",
                "POSTGRES_DB=chronicle",
                "--health-cmd",
                "pg_isready -h 127.0.0.1 -U postgres -d chronicle",
                "--health-interval",
                "1s",
                "--health-timeout",
                "2s",
                "--health-retries",
                "60",
                POSTGRES_IMAGE,
            )
            deadline = time.monotonic() + 90
            while (
                docker("inspect", "--format", "{{.State.Health.Status}}", container, timeout=5)
                != "healthy"
            ):
                if time.monotonic() >= deadline:
                    raise DemoError("Demo database startup timed out. Check Docker and retry.")
                time.sleep(1)
            os.environ["CHRONICLE_DB_PORT"] = docker(
                "inspect",
                "--format",
                '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}',
                container,
                timeout=5,
            )
            with db.connect("admin") as admin:
                db.provision_roles(admin)
                db.migrate(admin)
            result = demo.run()
            values = {
                item["case"]: item["report"]["rows"][0]["value"] for item in result["reports"]
            }
            expected = {
                "before": "10.000000000000",
                "after": "13.000000000000",
                "old": "10.000000000000",
                "mean": "102.666666666667",
            }
            if values != expected:
                raise DemoError("Demo report values differ from expected results.")
            print("Synthetic inputs, production SQL and report calculations\n")
            print("January employment change (thousand persons)")
            print(f"  Before correction       {values['before']}")
            print(f"  After correction        {values['after']}")
            print(f"  Old snapshot, queried again  {values['old']}")
            print(f"\nThree-month production mean   {values['mean']}")
        finally:
            os.chdir(previous)
            try:
                docker("rm", "--force", "--volumes", container, timeout=30)
            except DemoError:
                try:
                    absent = not docker(
                        "ps", "--all", "--quiet", "--filter", f"name=^/{container}$", timeout=5
                    )
                except DemoError:
                    absent = False
                if not absent:
                    raise DemoError(
                        f"Cleanup needed: docker rm --force --volumes {container}"
                    ) from None
    print("Demo container and its volume removed.")
    print("PASS: revised values, frozen snapshot and all four report replays verified.")


def main():
    try:
        run()
    except DemoError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": public_error(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
