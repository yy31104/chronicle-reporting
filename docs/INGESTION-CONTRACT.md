# Resumable ingestion and parser generations

Version: `chronicle.ingestion.v1`, 2026-10-29.

`ingestion.import_sources(importer, publisher, dataset_id, snapshot_id, source_ids,
expected_parent, request_id)` coordinates the direct-source adapter and durable
object store. The request UUID belongs to the caller and must be retained for
retries. Importer and publisher are separate least-privilege database connections
to the same database, in idle autocommit mode. No outer transaction may pretend
that a filesystem publication and database commit form one atomic resource.

For each of the explicit source IDs, the service fetches and parses the allowlisted
archive, durably stages its bytes, then saves an immutable acquisition document
containing the parsed revisions, original receipt, rights policy, source manifest,
object hash and ticket/worker identity. Only then does it import a complete batch
and attach the retained raw evidence. After all requested sources succeed, the
object publisher atomically publishes the parent snapshot's batch union plus the
new batches. All bytes and current rights must pass the publication closure check.
Fetch failure cannot advance the pointer or synthesize the missing source.

The optional `local_files={source_id: Path(...)}` keyword uses saved original HTML
for those requested source IDs. Its request identity binds the file hash and
explicit operator-supplied acquisition method; the source URI is still fixed by
the allowlist. The actual local read time becomes the first receipt. The original
receipt survives resumed import and parser repair. See the [source contract](SOURCE-CONTRACT.md)
for the authenticity boundary, bounded file checks and same-file retry rule.

A session advisory lock serializes identical request UUIDs across workers. The
lock is released if the connection closes. Request documents bind dataset,
snapshot, source IDs, expected parent and parser identity; changed fields reject
with `INGESTION_IDENTITY_CONFLICT`. Acquisition records are append-only. A retry
after acquisition or batch commit verifies the retained bytes and current rights,
reuses the exact parsed rows and receipt, then finishes the outstanding steps.
It does not refetch and create an arbitrary new receipt under the same batch ID.

A completed request replays the old snapshot even after the current pointer moves
or a newer parser is installed. It performs current rights/corruption checks and
closes any raw tickets left active by a response-lost process. It does not reexecute
the old parser. An incomplete request remains bound to its original parser identity;
changing that identity cannot silently reinterpret its half-completed work.

Failures before the acquisition record is saved can leave a registered live ticket
without a retained ingestion acquisition. The object protocol preserves such work
until a supervisor establishes that its worker terminated; it never guesses from
an elapsed TTL. Known partial requests retain their tickets for explicit resume.
Automatic published-object garbage collection remains disabled. Backup includes
ready objects referenced by active tickets, so acquired-but-unpublished requests
can resume after an isolated restore.

## Parser repair

`repair_generation(importer, publisher, predecessor_dataset, predecessor_snapshot,
new_dataset_id, snapshot_id, defect_note, request_id)` reparses the original raw
bytes into a new dataset. It first checks the exact old snapshot's current rights
and object hashes, then uses the retained source receipt and reviewed policy.
It performs no network request and never replaces old normalized rows or reports.

Migration 008 creates immutable request, acquisition and interpretation-generation
records. A generation pins the first request, parser source fingerprint, previous
dataset/snapshot when applicable, a required defect note and the actual database
`interpreted_at` timestamp. The original source receipt and the new interpretation
timestamp are separate. An old 2024 release imported in 2026 does not become local
2024 knowledge merely because a parser is repaired.

The source namespace, release identity and source availability retain their
publisher meanings. A parser correction is an interpretation change under a new
dataset identity; it is not relabeled a source revision. Its manifest retains the
original raw SHA256, receipt, prior parser version, predecessor snapshot, defect
note and reinterpretation time. It also preserves the original acquisition channel
and operator-asserted authenticity, where present. A generation cannot adopt a dataset that already
contains untracked batches/snapshots. A different parser cannot append to an
existing generation; it returns `PARSER_GENERATION_REQUIRED`.

Repair currently supports the four narrow direct-source adapters. If a source was
reobserved more than once in a predecessor snapshot, the first occurrence by
stored batch ID/object hash order supplies its retained raw bytes; the entire
parent snapshot closure is still verified before choosing. This deterministic
choice does not claim all reobservations are newly acquired. Unsupported metadata
returns `UNSUPPORTED_REPAIR_SOURCE`.

## Verification

The PostgreSQL suite exercises cumulative publication and unchanged old report
replay; crashes after acquisition, batch commit and publication commit; concurrent
same-request workers; changed-identity rejection; completed replay after a parser
upgrade; failed second-source acquisition; and current rights/corruption checks
before repair parsing. Owner-level mutations of all three new tables produce
SQLSTATE 55000.

The repair test deliberately uses a faulty old parser that takes a percent-change
column as an index level. Its retained January mean is `68.533333333333`. Repair
from the exact retained original HTML in a new dataset gives `102.666666666667`;
the old report bytes and receipt remain unchanged. This is an injected regression
test, not a claim that the publisher supplied the wrong column or that production
has already needed such a repair.

Executed on 2026-10-29 against disposable real PostgreSQL databases:

```text
UV_CACHE_DIR=.cache/uv UV_OFFLINE=1 uv run --locked pytest --tb=short \
  tests/postgres/test_ingestion.py tests/postgres/test_recovery_ingestion.py
16 passed in 7.66s
```

The first run exposed shared test-policy revocation across otherwise separate
datasets. The test fixtures now use distinct policy documents, matching the fact
that revocation intentionally affects every artifact using the same policy hash.
Production rights revocation behavior was retained. Each test now isolates the
entire database and object root, not merely one dataset. The real backup/restore
regression stops after acquisition retention, backs up zero published objects plus
one active acquired object, restores a fresh database, and resumes the same UUID
without refetching or changing the receipt. The original database stays unfinished.
Network success/failure is
reported separately in `docs/SOURCE-CONTRACT.md`; no BLS 403 response is converted
into a synthetic successful import.
