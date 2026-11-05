"""Local PostgreSQL configuration and versioned migration command.

This is a single-user development deployment. Runtime connections never use the
admin identity. Tests create an isolated database and remove only that database.
"""

import argparse
import hashlib
import os
import re
import secrets
import stat
from importlib.resources import files
from pathlib import Path

import psycopg
from psycopg import sql

ROLES = ("importer", "publisher", "reader")
_CONFIGURATION_LIMIT = 64 * 1024


def _read_configuration(path: Path, *, private: bool, optional: bool = False) -> dict[str, str]:
    """Read bounded literal assignments through anchored, non-symlink descriptors."""
    directory = descriptor = None
    try:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("absolute path required")
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for component in path.parts[1:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        try:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            if optional:
                return {}
            raise
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _CONFIGURATION_LIMIT:
            raise ValueError("invalid file")
        if private and (metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise ValueError("private ownership required")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(_CONFIGURATION_LIMIT + 1)
        if len(raw) > _CONFIGURATION_LIMIT or b"\x00" in raw:
            raise ValueError("invalid content")
        result = {}
        for line in raw.decode("utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError("assignment required")
            key, value = line.split("=", 1)
            if not re.fullmatch(r"CHRONICLE_[A-Z0-9_]+", key):
                if private:
                    raise ValueError("unsupported key")
                continue  # Legacy dotenv may also contain unrelated Compose settings.
            if key in result or (private and key == "CHRONICLE_ENV_FILE"):
                raise ValueError("duplicate or nested configuration")
            result[key] = value
        return result
    except (OSError, ValueError):
        raise RuntimeError("Chronicle configuration file is missing, unsafe or invalid.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def configure_local() -> None:
    """Create random local credentials once, without printing or replacing them."""
    destination = Path(".env")
    lines = ["CHRONICLE_DB_PORT=55473"]
    for name in ("admin", *ROLES):
        lines.append(f"CHRONICLE_DB_{name.upper()}_PASSWORD={secrets.token_urlsafe(32)}")
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Local .env already exists; preserved.")
        return
    with os.fdopen(descriptor, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    print("Created private local .env credentials.")


def configuration() -> dict[str, str]:
    """Legacy defaults, an explicit private external file, then process overrides.

    A configured external file must be available and valid; missing secrets never
    silently select a different file. Values are literal, without shell expansion.
    """
    result = _read_configuration(Path(".env").absolute(), private=False, optional=True)
    external = os.environ.get("CHRONICLE_ENV_FILE", result.get("CHRONICLE_ENV_FILE"))
    if external is not None:
        result.update(_read_configuration(Path(external), private=True))
    result.update({key: value for key, value in os.environ.items() if key.startswith("CHRONICLE_")})
    return result


def connect(role: str, *, database: str | None = None) -> psycopg.Connection:
    if role not in ("admin", *ROLES):
        raise ValueError("Unknown local connection role")
    config = configuration()
    password = config.get(f"CHRONICLE_DB_{role.upper()}_PASSWORD")
    if not password:
        raise RuntimeError("Local credentials missing; run make dev-config")
    try:
        return psycopg.connect(
            host=config.get("CHRONICLE_DB_HOST", "127.0.0.1"),
            port=int(config.get("CHRONICLE_DB_PORT", "55473")),
            dbname=database or config.get("CHRONICLE_DB_NAME", "chronicle"),
            user="postgres" if role == "admin" else f"chronicle_{role}",
            password=password,
            autocommit=True,
            connect_timeout=5,
            application_name=f"chronicle-{role}",
            options="-c timezone=UTC -c statement_timeout=15000 -c lock_timeout=5000",
        )
    except psycopg.OperationalError:
        # Driver traceback source can include connection kwargs. Never expose it
        # for unavailable local services or incorrect development credentials.
        raise RuntimeError(
            "Chronicle PostgreSQL connection failed; check make dev-up, local credentials "
            "and localhost network access. No database test has run."
        ) from None


def provision_roles(admin: psycopg.Connection) -> None:
    """Admin-only setup; preserve existing attributes and reject unsafe roles."""
    config = configuration()
    with admin.transaction():
        for name in ("owner", *ROLES):
            role = f"chronicle_{name}"
            existing = admin.execute(
                "SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolcanlogin "
                "FROM pg_roles WHERE rolname = %s",
                (role,),
            ).fetchone()
            if existing is None:
                admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            elif any(existing[:4]) or (name == "owner" and existing[4]):
                raise RuntimeError(f"Unsafe pre-existing {role}; refusing to reuse")
            if admin.execute(
                "SELECT 1 FROM pg_auth_members AS m JOIN pg_roles AS r ON r.oid=m.member "
                "WHERE r.rolname=%s",
                (role,),
            ).fetchone():
                raise RuntimeError(f"Unsafe role membership for {role}; refusing to reuse")
            if name != "owner":
                admin.execute(
                    sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role),
                        sql.Literal(config[f"CHRONICLE_DB_{name.upper()}_PASSWORD"]),
                    )
                )


def migrate(admin: psycopg.Connection, *, through: int | None = None) -> None:
    """Apply packaged migrations atomically and detect changed applied bytes."""
    with admin.transaction():
        admin.execute("SELECT pg_advisory_xact_lock(384920761)")
        admin.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        database = admin.info.dbname
        admin.execute(
            sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        admin.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO chronicle_owner").format(
                sql.Identifier(database)
            )
        )
        admin.execute("SET LOCAL ROLE chronicle_owner")
        admin.execute("CREATE SCHEMA IF NOT EXISTS chronicle AUTHORIZATION chronicle_owner")
        admin.execute(
            "CREATE TABLE IF NOT EXISTS chronicle.schema_migration "
            "(name text PRIMARY KEY, sha256 text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT statement_timestamp())"
        )
        for path in sorted(
            files("chronicle").joinpath("sql").iterdir(), key=lambda path: path.name
        ):
            if not path.name[:3].isdigit() or not path.name.endswith(".sql"):
                continue
            if through is not None and int(path.name[:3]) > through:
                continue
            body = path.read_text()
            digest = hashlib.sha256(body.encode()).hexdigest()
            previous = admin.execute(
                "SELECT sha256 FROM chronicle.schema_migration WHERE name = %s", (path.name,)
            ).fetchone()
            if previous:
                if previous[0] != digest:
                    raise RuntimeError(f"Applied migration changed: {path.name}")
                continue
            admin.execute(body)
            admin.execute(
                "INSERT INTO chronicle.schema_migration(name, sha256) VALUES (%s, %s)",
                (path.name, digest),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("configure", "migrate"))
    args = parser.parse_args()
    if args.command == "configure":
        configure_local()
        return
    with connect("admin") as admin:
        provision_roles(admin)
        migrate(admin)
        print("Chronicle migrations applied; runtime roles are separate from admin.")


if __name__ == "__main__":
    main()
