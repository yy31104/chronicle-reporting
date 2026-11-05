# Chronicle PostgreSQL contract

Version: `chronicle.postgres.c1.v2`, 2026-10-25. Scope: local synthetic C1-PR02
with review corrections.

This document describes the implementation in [schema](../src/chronicle/sql/001_schema.sql),
[protected functions](../src/chronicle/sql/002_publication.sql),
[READ COMMITTED sealing amendment](../src/chronicle/sql/003_seal_read_committed.sql),
[selection SQL](../src/chronicle/sql/select_context.sql), and
[Python store](../src/chronicle/store.py). It extends the persistence and
current-pointer boundaries of [WORKING-CONTRACT.md](WORKING-CONTRACT.md); its source
time, uncertainty and numerical rules continue to apply. It is not an execution
record or a claim that all acceptance tests have passed.

## Local deployment and transaction boundary

The configured server target is PostgreSQL 18.6. [compose.yaml](../compose.yaml)
pins `postgres:18` to digest
`sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722`.
The Python dependency and lock pin `psycopg[binary]==3.3.3`.

The `chronicle` Compose project contains one PostgreSQL service, exposes it on
`127.0.0.1:55473` by default, and uses its own named volume. Development commands
operate on that project; they do not stop, remove or reconfigure other containers.
`make dev-down` stops its service and preserves the volume. Local credentials are
generated once in the ignored `.env` file with mode `0600` and are not printed.
This is local development isolation, not a production deployment specification.

Connections use autocommit outside explicit `conn.transaction()` blocks; import,
publication, context creation and selection open bounded transaction scopes. An
exception rolls back that scope. Tests must inspect committed state through actual
separate connections. This follows Psycopg's documented distinction between
[autocommit and transaction contexts](https://www.psycopg.org/psycopg3/docs/basic/transactions.html).

The supported write/retry contract uses READ COMMITTED. Migration 003 explicitly
requires that named isolation level for every `seal_batch` call, including replay.
Other isolation settings raise `P0001 / SEAL_REQUIRES_READ_COMMITTED` before lookup
or locking; the function does not silently change the caller's isolation level.

The administrator provisions roles and applies packaged migrations as
`chronicle_owner`, a non-login owner. A migration ledger stores file hashes and
rejects changes to already applied migration bytes. Application operations use
their own runtime connections.

## Runtime authority

| Identity | Allowed operations | Required trust |
|---|---|---|
| `chronicle_importer` | Select import tables; insert dataset IDs, source namespaces, series, releases, revisions, staging batch headers and occurrences; call `seal_batch`; execute pure constraint helpers | Python admission and fingerprint comparisons correctly describe the proposed source input |
| `chronicle_publisher` | Call `publish_snapshot`; no direct table DML | Caller selects the intended complete batch set and fixture hash |
| `chronicle_reader` | Select all application tables, including staging data; call `create_context` | Trusted local analyst/application role, with no tenant or dataset authorization boundary |
| `chronicle_owner` / administrator | Migration ownership and privileged maintenance | Trusted setup identities, outside runtime protection claims |

Runtime roles cannot update, delete or truncate historical records or directly
insert snapshots, snapshot membership or contexts. The importer cannot set the
dataset pointer or batch state by direct INSERT/UPDATE. Table privileges and
rejecting mutation triggers enforce these limits. Owner-executed transitions may
change only the dataset pointer or a batch from staging to validated with its
identity/header unchanged.

The sealing, publication and context functions are `SECURITY DEFINER`, use fixed
`search_path=pg_catalog`, and qualify application objects. PUBLIC has no execution
grant; each exposed function is granted only to its designated role. The occurrence
guard is a definer trigger with no runtime direct execution grant. These controls
follow PostgreSQL's guidance for [safe definer functions](https://www.postgresql.org/docs/18/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY).
They do not protect against an administrator changing grants or replacing functions.

## Stored inputs and import contract

Composite keys retain dataset scope throughout source namespaces, series, releases,
revisions, batches, occurrences and snapshot membership. A source namespace has one
order rule; a release foreign key includes that rule. Distinct releases cannot share
a source order within a dataset/namespace. Date-ordinal ordering must equal the
known source date's ordinal. Documented sequence values are supplied evidence,
not chronology inferred by the database.

Revision identity includes dataset, source namespace, series, observation month and
source revision key. Content retains status, lexical value and unit metadata.
`value` records require a valid decimal; missing and withdrawn records require null.
Months are valid calendar starts. Source availability accepts date or unknown;
unsupported instant requests remain rejected by the C1 input boundary.

`revision.value` is text. SQL accepts the same plain ASCII decimal grammar, maximum
128 characters, maximum 12 fractional digits and NUMERIC(38,12)-compatible magnitude
as the pure input contract. The range check casts to unconstrained exact `numeric`
only after lexical validation. It does not store `NUMERIC(38,12)` or silently round
extra source digits. For example, `+00100.0000` retains those exact characters.
This slice selects source values; it does not implement production report formulas.

Each batch header binds `(dataset_id, batch_id)` to a SHA-256 content fingerprint
and expected occurrence count. Python builds the fingerprint from a stable sorted
serialization of the full typed batch input. An identical validated retry returns
the same batch ID; different content or an existing incomplete state rejects.
For reused revision/release identities, Python inserts if absent and compares all
stored content before accepting the occurrence. A failed import rolls back rather
than recording a fictitious persisted quarantine.

The database validates fingerprint syntax, immutable headers, composite references
and counts. It does **not** independently recompute the fingerprint or establish
source truth. A trusted caller bypassing Python can supply a false fingerprint;
the runtime permission boundary is not a replacement for import admission.

An occurrence belongs to one batch and one complete revision key, with a supplied
receipt instant. Fixture receipt clocks remain synthetic. The occurrence insertion
trigger locks the batch row and accepts only staging batches. `seal_batch` acquires
the same lock, requires actual count to equal expected count, and marks the batch
validated. Empty batches are allowed when expected count is zero. A repeated seal
is idempotent under READ COMMITTED. Holding the common lock through commit prevents
an insert from slipping into a validated batch and changing a previously pinned
answer. The count check additionally depends on READ COMMITTED taking a fresh
statement snapshot after the lock wait. REPEATABLE READ can miss a just-committed
occurrence while still acquiring that lock; the original function could therefore
seal an overfull directly staged batch. The explicit isolation guard closes that
path. The PostgreSQL [isolation documentation](https://www.postgresql.org/docs/18/transaction-iso.html)
describes the different statement/transaction snapshot rules. The old behavior is
reproduced only in a disposable database by the regression suite.

Applied migrations 001/002 remain byte-identical. Migration 003 replaces the
protected function without resetting data or changing its identity/owner/grants.

## Complete explicit snapshots

`publish_snapshot(dataset, snapshot, batch_ids, manifest_hash, expected_current)`
accepts a duplicate-free explicit set of validated batches in the same dataset.
It locks the dataset row, checks the expected-current value, then writes the
snapshot, exact membership and current pointer in one transaction. Missing or
staging batches reject; failure leaves no partial snapshot or pointer change.

Snapshot identity is bound to its sorted batch set and fixture hash. Reusing the
identity with either changed rejects. Replaying an already committed identical
snapshot returns its original ID even after a newer snapshot exists, without
rewinding the current pointer. This supports a retry after a lost response.

C1 membership is a complete selection, not an implicit cumulative union with the
previous pointer. A new snapshot may omit older batches; G3's `S_REOBSERVED` is an
intentional example. Existing snapshots and contexts retain their original sets.
Parent-union/rebase semantics are later C2 work.

The test fixture helper hashes a sorted-key JSON serialization of the input fixture
and supplies that value as `fixture_manifest_sha256`. The database checks its shape
and replay equality. It does not locate or hash a filesystem object, verify an
external manifest, prove source truth, or provide C2 file-promotion, pinning, garbage
collection, rights or power-loss guarantees. `snapshot.created_at` is a database
statement timestamp, not an exact commit-time witness.

## Immutable contexts and historical selection

`create_context` stores a UUID, dataset/snapshot pin, mode, requested namespace and
series, ordered distinct month grid, optional source cutoff and paired expected
unit/metadata fields. Unknown requested series are valid: their requested cells
remain present with a nonnumeric result rather than failing a series foreign key.

The normal `create_context` wrapper uses a pure `Query` with an explicit snapshot.
`resolve_current_context` is a separate service operation: latest-corrected and
source-as-of may resolve the current pointer once; system-snapshot requires an
explicit ID. Source-as-of still requires its date-exclusive cutoff. The stored
context always contains a real snapshot ID. Replay compares retained context
parameters and uses the original pin for an implicit-current retry. It does not
silently replace that pin with today's current pointer.

Selection never consults the current pointer. It starts with the immutable
context's ordered requested grid and considers a revision only when an occurrence
belongs to that snapshot's batch set. Known-time eligibility uses the exclusive
date cutoff. A membership-restricted higher-order unknown revision blocks an
authoritative old answer under F3. Withdrawal, explicit missing, no eligible row,
unknown availability and unit mismatch preserve their distinct statuses and null
rules. A latest view may return an unknown-time value with its warning flag.

Direct reader SELECT can inspect staging rows, but the production selector cannot
use them without published membership. Scope keys prevent accidental cross-dataset
joins; they do not grant one user permission or tenant to access another dataset.

## Acceptance and remaining work

The database suite requires real-role SQL-versus-oracle checks,
frozen expected values, atomic rejection/replay, immutable context behavior,
concurrent publication and a mutation removing the SQL unknown-time guard.
Actual commands, environment and outcomes must be recorded after execution; this
contract supplies no passing counts.

C1-PR03 report calculations and delivery, C2 real-source/object durability and
retention, multi-user authorization, deployment, incremental analysis and evaluated
external generation remain separate work. Implementation and verified behavior
continue to be recorded independently.
