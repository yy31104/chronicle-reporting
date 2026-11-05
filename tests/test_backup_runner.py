import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chronicle import backup_runner as runner
from chronicle.errors import ContractError


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path.resolve() / "runtime"
    root.mkdir(mode=0o700)
    return root


@pytest.fixture
def created(monkeypatch):
    calls = []

    def make_backup(destination, *, database, root):
        calls.append((destination, database, root))
        destination.mkdir(mode=0o700)
        (destination / "database.dump").write_bytes(b"test dump, not a real PostgreSQL backup")
        manifest = {
            "backup_schema": "chronicle.backup.v1",
            "database": database,
            "files": {
                "database.dump": hashlib.sha256(
                    (destination / "database.dump").read_bytes()
                ).hexdigest()
            },
        }
        (destination / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    monkeypatch.setattr(runner, "backup", make_backup)
    return calls


def test_daily_job_retains_fourteen_and_does_not_repeat_same_local_day(runtime, created):
    start = datetime(2026, 10, 1, 12, tzinfo=UTC)
    first = runner.run_daily(runtime, now=start)
    assert first["status"] == "completed"
    duplicate = runner.run_daily(runtime, now=start + timedelta(hours=1))
    assert duplicate["status"] == "already_backed_up"
    assert len(created) == 1
    for day in range(1, 16):
        result = runner.run_daily(runtime, now=start + timedelta(days=day))
    assert result["retained"] == 14
    assert len(list((runtime / "backups" / "scheduled").iterdir())) == 14
    assert not Path(first["backup"]).exists()
    assert len(created) == 16
    assert (runtime / "backup-status.json").stat().st_mode & 0o777 == 0o600


def test_unknown_partial_symlink_extra_and_corrupt_backups_are_never_pruned(runtime, created):
    start = datetime(2026, 10, 1, 12, tzinfo=UTC)
    old = Path(runner.run_daily(runtime, keep=1, now=start)["backup"])
    (old / "unexpected.txt").write_text("must preserve unknown data")
    directory = old.parent
    partial = directory / ("daily-20260901T120000Z-" + "a" * 32)
    partial.mkdir()
    (partial / "database.dump").write_text("incomplete")
    unrelated = directory / "manual-backup"
    unrelated.mkdir()
    outside = runtime / "outside"
    outside.mkdir()
    (outside / "valuable").write_text("preserve")
    link = directory / ("daily-20260902T120000Z-" + "b" * 32)
    link.symlink_to(outside, target_is_directory=True)
    corrupt = Path(runner.run_daily(runtime, keep=1, now=start + timedelta(days=1))["backup"])
    (corrupt / "database.dump").write_text("changed")
    result = runner.run_daily(runtime, keep=1, now=start + timedelta(days=2))
    assert result["pruned"] == []
    assert result["preserved_unmanaged_entries"] == 5
    assert all(path.exists() for path in (old, partial, unrelated, outside, link, corrupt))


def test_failed_new_backup_never_prunes_old_good_copy(runtime, created, monkeypatch):
    start = datetime(2026, 10, 1, 12, tzinfo=UTC)
    old = Path(runner.run_daily(runtime, keep=1, now=start)["backup"])

    def failure(*args, **kwargs):
        raise RuntimeError("private-password-and-driver-details")

    monkeypatch.setattr(runner, "backup", failure)
    with pytest.raises(RuntimeError):
        runner.run_daily(runtime, keep=1, now=start + timedelta(days=1))
    assert old.exists()
    state = (runtime / "backup-status.json").read_text()
    assert json.loads(state)["status"] == "failed"
    assert "private-password" not in state


def test_clock_rollback_creates_today_and_preserves_future_copy(runtime, created):
    future = datetime(2027, 10, 1, 12, tzinfo=UTC)
    future_path = Path(runner.run_daily(runtime, keep=1, now=future)["backup"])
    today = datetime(2026, 10, 1, 12, tzinfo=UTC)
    result = runner.run_daily(runtime, keep=1, now=today)
    assert result["status"] == "completed"
    assert result["preserved_unmanaged_entries"] == 1
    assert result["pruned"] == []
    assert future_path.exists()
    assert Path(result["backup"]) != future_path
    assert runner.run_daily(runtime, keep=1, now=today)["status"] == "already_backed_up"


def test_marker_bound_invalid_manifest_is_preserved(runtime, created):
    today = datetime(2026, 10, 1, 12, tzinfo=UTC)
    old = Path(runner.run_daily(runtime, keep=1, now=today)["backup"])
    (old / "manifest.json").write_text('{"backup_schema":"wrong"}')
    marker_path = old / "scheduled-complete.json"
    marker = json.loads(marker_path.read_text())
    marker["manifest_sha256"] = hashlib.sha256((old / "manifest.json").read_bytes()).hexdigest()
    marker_path.write_text(json.dumps(marker))
    result = runner.run_daily(runtime, keep=1, now=today + timedelta(days=1))
    assert result["preserved_unmanaged_entries"] == 1
    assert old.exists()


def test_concurrent_job_does_not_create_backup_or_overwrite_status(runtime, created):
    state = runtime / "backup-status.json"
    state.write_text('{"status":"previous"}')
    with (runtime / ".backup-run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert runner.run_daily(runtime) == {"status": "already_running"}
    assert not created
    assert json.loads(state.read_text()) == {"status": "previous"}


def test_database_change_preserves_other_database_backups(runtime, created):
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    previous = Path(runner.run_daily(runtime, keep=1, now=now)["backup"])
    result = runner.run_daily(runtime, keep=1, database="chronicle_recovered", now=now)
    assert result["status"] == "completed"
    assert result["preserved_unmanaged_entries"] == 1
    assert previous.exists()
    assert created[-1][1] == "chronicle_recovered"


@pytest.mark.parametrize("keep", [0, -1, 366, True])
def test_invalid_retention_cannot_trigger_deletion(runtime, created, keep):
    with pytest.raises(ContractError, match="Retain"):
        runner.run_daily(runtime, keep=keep)
    assert not created


def test_private_configuration_is_data_not_shell_and_rejects_foreign_keys(runtime):
    path = runtime / "backup.env"
    path.write_text("CHRONICLE_DB_ADMIN_PASSWORD=$(do-not-execute)\n")
    path.chmod(0o600)
    assert runner.load_environment(runtime)["CHRONICLE_DB_ADMIN_PASSWORD"] == "$(do-not-execute)"
    path.write_text("CHRONICLE_DB_ADMIN_PASSWORD=private\nUNEXPECTED=1\n")
    with pytest.raises(ContractError) as error:
        runner.load_environment(runtime)
    assert error.value.code == "INVALID_BACKUP_CONFIG"
    path.write_text("CHRONICLE_DB_ADMIN_PASSWORD=private\n")
    path.chmod(0o644)
    with pytest.raises(ContractError) as error:
        runner.load_environment(runtime)
    assert error.value.code == "INSECURE_BACKUP_CONFIG"


def test_configuration_symlink_and_world_accessible_runtime_fail_closed(runtime):
    target = runtime / "elsewhere"
    target.write_text("CHRONICLE_DB_ADMIN_PASSWORD=private\n")
    target.chmod(0o600)
    (runtime / "backup.env").symlink_to(target)
    with pytest.raises(OSError):
        runner.load_environment(runtime)
    runtime.chmod(0o755)
    with pytest.raises(ContractError) as error:
        runner.run_daily(runtime)
    assert error.value.code == "INSECURE_BACKUP_PATH"


def test_preflight_failure_persists_safe_status(runtime, monkeypatch, capsys):
    path = runtime / "backup.env"
    path.write_text("CHRONICLE_DB_ADMIN_PASSWORD=private-secret\nUNKNOWN=value\n")
    path.chmod(0o600)
    monkeypatch.setattr("sys.argv", ["backup_runner", "--runtime-root", str(runtime)])
    assert runner.main() == 1
    output = capsys.readouterr().out
    assert "private-secret" not in output
    assert json.loads(output)["error"]["code"] == "INVALID_BACKUP_CONFIG"
    assert json.loads((runtime / "backup-status.json").read_text())["status"] == "failed"


def test_main_drops_inherited_database_secrets_and_uses_private_root(runtime, monkeypatch, capsys):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    path = runtime / "backup.env"
    path.write_text("CHRONICLE_DB_ADMIN_PASSWORD=private-secret\n")
    path.chmod(0o600)
    monkeypatch.chdir(runtime)
    monkeypatch.setenv("CHRONICLE_DB_READER_PASSWORD", "unneeded-secret")
    monkeypatch.setenv("CHRONICLE_DB_NAME", "inherited-wrong-target")
    monkeypatch.setattr("sys.argv", ["backup_runner", "--runtime-root", str(runtime)])
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/local/bin/docker")

    def job(root, *, keep, database):
        assert Path.cwd() == runtime
        assert database == "chronicle"
        assert "CHRONICLE_DB_READER_PASSWORD" not in os.environ
        assert os.environ["CHRONICLE_DB_ADMIN_PASSWORD"] == "private-secret"
        return {"status": "completed"}

    monkeypatch.setattr(runner, "run_daily", job)
    assert runner.main() == 0
    assert json.loads(capsys.readouterr().out) == {"status": "completed"}
