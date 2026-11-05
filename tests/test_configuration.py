"""Secret configuration admission and Compose invocation boundaries."""

import os
from pathlib import Path

import pytest

from chronicle import db


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith("CHRONICLE_"):
            monkeypatch.delenv(key)


def private_file(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    path.chmod(0o600)
    return path


def test_legacy_configuration_and_process_override(monkeypatch):
    Path(".env").write_text("# local development\nCHRONICLE_DB_PORT=55473\n")
    assert db.configuration()["CHRONICLE_DB_PORT"] == "55473"
    monkeypatch.setenv("CHRONICLE_DB_PORT", "55474")
    assert db.configuration()["CHRONICLE_DB_PORT"] == "55474"


def test_external_file_literal_values_spaces_and_precedence(tmp_path, monkeypatch):
    path = private_file(
        tmp_path / "Application Support" / "database.env",
        "CHRONICLE_DB_PORT=55473\nCHRONICLE_DB_ADMIN_PASSWORD=$(not-a-command) # literal\n",
    )
    Path(".env").write_text(f"CHRONICLE_ENV_FILE={path}\nCHRONICLE_DB_PORT=1\n")
    config = db.configuration()
    assert config["CHRONICLE_DB_ADMIN_PASSWORD"] == "$(not-a-command) # literal"
    assert config["CHRONICLE_DB_PORT"] == "55473"
    monkeypatch.setenv("CHRONICLE_DB_PORT", "55475")
    assert db.configuration()["CHRONICLE_DB_PORT"] == "55475"


def test_environment_selects_external_file_over_local_pointer(tmp_path, monkeypatch):
    path = private_file(tmp_path / "external.env", "CHRONICLE_DB_NAME=chosen\n")
    Path(".env").write_text("CHRONICLE_ENV_FILE=/missing/local-pointer.env\n")
    monkeypatch.setenv("CHRONICLE_ENV_FILE", str(path))
    assert db.configuration()["CHRONICLE_DB_NAME"] == "chosen"


@pytest.mark.parametrize("value", ["", "relative.env", "/missing/chronicle-secrets.env"])
def test_explicit_bad_path_never_silently_falls_back(value, monkeypatch):
    Path(".env").write_text("CHRONICLE_DB_ADMIN_PASSWORD=old-secret\n")
    monkeypatch.setenv("CHRONICLE_ENV_FILE", value)
    with pytest.raises(RuntimeError, match="configuration"):
        db.configuration()


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o700])
def test_external_file_requires_private_mode(tmp_path, monkeypatch, mode):
    path = private_file(tmp_path / "secrets.env", "CHRONICLE_DB_ADMIN_PASSWORD=secret\n")
    path.chmod(mode)
    monkeypatch.setenv("CHRONICLE_ENV_FILE", str(path))
    with pytest.raises(RuntimeError, match="configuration"):
        db.configuration()


def test_external_file_owner_must_be_current_user(tmp_path, monkeypatch):
    path = private_file(tmp_path / "secrets.env", "CHRONICLE_DB_ADMIN_PASSWORD=secret\n")
    monkeypatch.setenv("CHRONICLE_ENV_FILE", str(path))
    actual_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: actual_uid + 1)
    with pytest.raises(RuntimeError, match="configuration"):
        db.configuration()


@pytest.mark.parametrize("ancestor", [False, True])
def test_external_file_rejects_symlinks(tmp_path, monkeypatch, ancestor):
    path = private_file(tmp_path / "real" / "database.env", "CHRONICLE_DB_NAME=secret\n")
    linked = tmp_path / "linked"
    linked.symlink_to(path.parent if ancestor else path)
    monkeypatch.setenv("CHRONICLE_ENV_FILE", str(linked / path.name if ancestor else linked))
    with pytest.raises(RuntimeError, match="configuration"):
        db.configuration()


def test_external_file_rejects_fifo_without_blocking(tmp_path, monkeypatch):
    path = tmp_path / "fifo"
    os.mkfifo(path, 0o600)
    monkeypatch.setenv("CHRONICLE_ENV_FILE", str(path))
    with pytest.raises(RuntimeError, match="configuration"):
        db.configuration()


@pytest.mark.parametrize(
    "contents",
    [
        "CHRONICLE_DB_NAME=a\nCHRONICLE_DB_NAME=b\n",
        "CHRONICLE_ENV_FILE=/nested.env\n",
        "PATH=/untrusted\n",
        "CHRONICLE_DB_ADMIN_PASSWORD=secret\x00\n",
        "not-an-assignment\n",
        "CHRONICLE_DB_ADMIN_PASSWORD=" + "x" * 65536,
    ],
    ids=["duplicate", "nested", "unknown-key", "nul", "malformed", "oversized"],
)
def test_external_file_rejects_ambiguous_or_unbounded_content(tmp_path, monkeypatch, contents):
    path = private_file(tmp_path / "secret.env", contents)
    monkeypatch.setenv("CHRONICLE_ENV_FILE", str(path))
    with pytest.raises(RuntimeError, match="configuration") as error:
        db.configuration()
    assert "secret" not in str(error.value)


def test_environment_only_container_configuration(monkeypatch):
    monkeypatch.setenv("CHRONICLE_DB_READER_PASSWORD", "runtime-only")
    assert db.configuration() == {"CHRONICLE_DB_READER_PASSWORD": "runtime-only"}


def test_compose_passes_arguments_and_loaded_environment_without_shell(tmp_path, monkeypatch):
    from chronicle import compose

    path = private_file(tmp_path / "secret.env", "CHRONICLE_DB_ADMIN_PASSWORD=private\n")
    Path(".env").write_text(f"CHRONICLE_ENV_FILE={path}\n")
    monkeypatch.setenv("PATH", "/existing-path")
    observed = {}

    def execute(program, arguments, environment):
        observed.update(program=program, arguments=arguments, environment=environment)

    monkeypatch.setattr(os, "execvpe", execute)
    compose.main(["-p", "chronicle", "config", "--quiet", "; echo not-a-shell"])
    assert observed["program"] == "docker"
    assert observed["arguments"] == [
        "docker",
        "compose",
        "-p",
        "chronicle",
        "config",
        "--quiet",
        "; echo not-a-shell",
    ]
    assert observed["environment"]["PATH"] == "/existing-path"
    assert observed["environment"]["CHRONICLE_DB_ADMIN_PASSWORD"] == "private"


def test_compose_configuration_failure_does_not_start_docker(monkeypatch, capsys):
    from chronicle import compose

    monkeypatch.setenv("CHRONICLE_ENV_FILE", "/missing/secret.env")
    monkeypatch.setattr(os, "execvpe", lambda *_: pytest.fail("must not start Docker"))
    assert compose.main(["up"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "configuration" in output.err
    assert "secret" not in output.err
