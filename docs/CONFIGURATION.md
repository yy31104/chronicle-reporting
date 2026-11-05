# Private local configuration

Chronicle supports a credential-free checkout `.env` that points to a private
configuration file outside a cloud-synchronized directory:

```text
CHRONICLE_ENV_FILE=/srv/chronicle/database.env
```

This is a configuration capability, not evidence that a live migration or password
rotation has happened. Deployment evidence must record those actions separately.

The external file contains the existing `CHRONICLE_DB_*` values and runtime paths,
including `CHRONICLE_DATA_ROOT`, `CHRONICLE_OBJECT_ROOT` and
`CHRONICLE_ACCESS_FILE`. It must be a regular file owned by the current user with
mode 0600. The path must be absolute; every directory component and the file itself
is opened without following symlinks. The reader allows at most 64KiB and does not
block on FIFOs. A missing or invalid explicitly configured file fails closed.

Values use literal UTF-8 `CHRONICLE_NAME=value` assignments. Spaces within values
are preserved. Blank lines and lines starting with `#` are accepted. Do not add
shell `export`, quotes around values, variable substitutions or inline comments:
they are not shell syntax here. A value such as `$(command)` is just a string.
External files reject duplicate keys, non-Chronicle keys, NUL bytes and a nested
`CHRONICLE_ENV_FILE`. No secret value is included in configuration errors.

Configuration precedence is checkout `.env`, then the explicitly selected external
file, then `CHRONICLE_*` process environment variables. The process environment can
also select the external file instead of the checkout pointer. Existing development
`.env` files continue to work without a pointer; `make dev-config` retains its
legacy create-once behavior and must not be used to replace an existing production
configuration. Application containers continue to use their explicit environment;
they do not need access to the host's complete external configuration file.

## Docker Compose

The Makefile's `dev-up`, `dev-down`, `private-up` and `private-down` targets use
`python -m chronicle.compose`. This wrapper loads the same configuration as the
CLI, adds it to the child environment and replaces itself with Docker Compose.
It passes arguments directly without a shell and introduces no service action of
its own. Compose still exposes only the reader password to the application and
only the admin password to the database container.

For a manual read-only configuration check from the repository:

```sh
CHRONICLE_APP_UID="$(id -u)" CHRONICLE_APP_GID="$(id -g)" PYTHONPATH=src \
  .venv/bin/python -m chronicle.compose -p chronicle \
  -f compose.yaml -f compose.private.yaml config --quiet
```

Direct `docker compose` does not interpret `CHRONICLE_ENV_FILE`; use the wrapper
or the Makefile. Avoid printing expanded Compose configuration because Docker's
own `config` output can contain credentials. Recovery acceptance already supplies
`db.configuration()` to its separately named Compose process. Backup uses its
separate admin-only `backup.env`, not the complete database configuration.

## Existing deployment migration and rotation

Treat relocation and revocation as distinct operations. Moving files out of
a shared synchronization service does not invalidate older copies of still-valid passwords.

1. Read the current configuration in memory; write the external file privately
   with create-only semantics, flush and fsync it and its parent. Preserve every
   existing setting. Verify role connections through the new file before changing
   the checkout. Never print the configuration or put passwords in shell arguments.
2. Atomically replace checkout `.env` with the path pointer, preserving mode 0600,
   and fsync its parent. Confirm that Compose `config --quiet` and the CLI read the
   expected configuration. A failed external read must stop the operation rather
   than recreate a development credential file.
3. If earlier cloud copies must become unusable, stage a complete new random
   password set in a private recovery file outside a shared synchronization service before altering roles.
   On an existing authenticated admin connection, change only the passwords of
   `postgres`, `chronicle_importer`, `chronicle_publisher` and `chronicle_reader`
   within one transaction. Preserve all role attributes, memberships and grants.
   A transaction failure leaves the old set active; a successful commit requires
   activating the staged external configuration and the admin-only backup file.
4. Recreate affected containers with the new environment without deleting the
   database volume. `POSTGRES_PASSWORD` does not change an initialized cluster's
   password; the actual SQL rotation is required. There can be a short interval
   of failed new app connections between database activation and app recreation.
5. Verify that all old passwords fail, each new role connects, readiness and a
   genuine saved report succeed, and a backup succeeds. Remove only obsolete
   local copies after that verification. A restart after interruption must inspect
   the staged recovery set and active database rather than guess which set won.

The focused configuration tests exercise legacy and environment-only operation,
precedence, literal values, unsafe paths/files, ownership/permissions, malformed or
oversized content, and direct Compose argument forwarding. Live rotation and
container/backup checks are separate deployment acceptance.
