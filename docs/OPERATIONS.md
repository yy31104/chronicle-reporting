# Local operations

Run `make private-up` to build and start the loopback application and database.
Keep migrations separate from startup. Runtime connections receive only the reader
credential; that role may append immutable contexts, reports, comparisons and pins.
It cannot import sources or publish snapshots. Import and publication use separate
CLI roles. Containers run without root privileges or a writable root filesystem.

## Configuration and access

A fresh checkout creates `.env` once through `make dev-config`. Keep it private.
For persistent use, set `CHRONICLE_ENV_FILE=/srv/chronicle/database.env` and place
runtime data in a service-owned directory. The external file requires current-user
ownership and mode0600. See [configuration](CONFIGURATION.md) for literal parsing,
precedence and password rotation.

`chronicle access-config --dataset DATASET` creates viewer/operator credentials
and a hashed access policy. Set `CHRONICLE_DATA_ROOT` before invoking it to use a
persistent runtime directory. Add only explicit datasets to the policy. The web
client keeps the token in memory and reloads the policy on requests. Use
`chronicle access-rotate` for rotation without printing tokens. Preserve0600 mode.

The service binds127.0.0.1:8787 and the database binds127.0.0.1:55473. Body size,
context months, evidence receipts, result documents, concurrent work and jobs are
bounded. Database queries use statement15s, lock5s and connection5s timeouts.
Cancellation stops actual database work, subject to its commit boundary. Durable
report identifiers survive application restart; in-memory job handles do not.

`/health/live` checks process liveness. `/health/ready` checks policy, database
connectivity and required tables through separate bounded capacity. It does not
hash every object. Actual reads, publication, `chronicle verify-objects` and backups
perform full streaming hash verification. Same-size corruption needs those checks.
Logs contain route templates, request IDs and timing, without payloads or secrets.

## Import and report checks

`chronicle import-sources --help` lists the four fixed archive source IDs.
Use a fresh request UUID and the expected parent snapshot. An identical retry
reuses captured bytes, receipt and publication identity. Conflicting content or a
changed parent returns a typed error; it never silently overwrites history.

A bounded direct-publisher HTML file can be supplied with
`--source-file SOURCE_ID=/absolute/local/file.html`. The source ID fixes the
publisher URL. This records local-file provenance and actual read time; it does
not authenticate the publisher. Modeled parser fixtures must not be admitted as
real acquisitions. Import failures do not create empty successful batches.

Source policies apply at import, read and export time. Parser repairs create a
new dataset interpretation generation while preserving the original raw bytes,
reports and source-use identities. Published objects have no automatic expiry.
Only explicitly ended staging work is eligible for conservative collection.

For the synthetic rounding example:

```sh
PYTHONPATH=src uv run --locked python scripts/seed_exact_difference_demo.py
```

Include both `chronicle-synthetic-v1` and `chronicle-synthetic-exact-v1` in the local
policy, then compare January2026, snapshotsS1/S2. The exact-comparison column must
show a once-rounded rational difference and explain any rounding gap. The retained
JSON export preserves canonical decimals.

## Backup and recovery

`chronicle backup /absolute/new/backup` combines an exported PostgreSQL snapshot
with verified immutable objects and report digests. It never overwrites an existing
backup and excludes credentials. Keep secrets separately and retain an encrypted
copy on independent storage for machine-loss recovery.

`chronicle restore BACKUP --into chronicle_NEW --object-root /existing/parent/NEW`
restores into fresh targets, verifies migration hashes, object closure and report
digests, and retains the result. It requires existing cluster roles; it does not
bootstrap a replacement cluster. Restore only trusted local dumps, since a dump
executes SQL. A hash manifest proves integrity rather than origin.

`chronicle restore-rehearsal BACKUP` checks recovery in an isolated database and
removes that test database afterward. `scripts/recovery_acceptance.py` additionally
starts an isolated service, checks reports/evidence, computes new reports, restarts
and replays them. The primary configuration is not switched by that script.

The optional `python -m chronicle.backup_runner --runtime-root /srv/chronicle
--keep 14` runner uses a private admin-only `backup.env`. Invoke it from one local
scheduler. It keeps the latest14 verified scheduler-owned daily copies, preserves
manual or unknown entries, and records a safe result in `backup-status.json`.
Repeated runs on the same local date avoid duplicate dumps. A missed run can make
a current copy later; it cannot reconstruct an earlier backup. A future-dated copy
after clock rollback does not suppress today's backup. Docker must be running.

`make dev-down` preserves the volume. Do not delete volumes or edit applied
migration bytes as an upgrade procedure. Public hosting, multi-host recovery and
large concurrent workloads require separate configuration and measurement.
