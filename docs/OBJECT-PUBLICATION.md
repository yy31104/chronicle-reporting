# Durable artifact publication and current rights

Implemented 2026-10-29 by additive migration `006_objects.sql` and
`chronicle.objects`. This extends the synthetic C1 store without changing applied
migrations or existing snapshots. New real-source ingestion uses a new managed
dataset; historical C1 snapshots remain readable under their existing contract.

## Filesystem and trust boundary

The configured `CHRONICLE_OBJECT_ROOT` defaults to `var/objects`. A digest maps to
`root/<first two hex characters>/<full SHA-256>`. The service owns the root and
prefix directories exclusively. It rejects symlink directories, world/group
writable directories, foreign-owned directories, non-regular object files and
symlink object files. Callers cannot supply an object path. Files and reads are
bounded to 32 MiB. A suitable deployment uses a persistent local POSIX filesystem;
cloud eviction, network mounts and Windows filesystems are not validated durability
backends. Configure a persistent service volume for deployment instead of relying
on an offloaded or shared synchronization directory.

Every new directory entry, temporary file content and final containing directory
is fsynced. Content promotion uses same-filesystem hard-link creation without
replacement; an existing hash is adopted only after its bytes and length match.
All registrations, promotions, publication checks and collection take the same
per-hash PostgreSQL advisory lock. Collisions only serialize unrelated digests.
The directory is service-owned; protection against a compromised host/owner is
outside this boundary. Hash verification detects missing/corrupt retained bytes.
Process-crash tests exercise these states; they do not simulate electrical power
loss or certify a storage device's flush guarantees.

## Admission and publication

1. `stage_bytes` opens an immutable-identity ticket with dataset, worker, rights
   policy and source metadata before writing bytes. It durably writes a bounded
   temporary file, commits the staged object and active ticket reference, promotes
   without replacement, then marks the object ready.
2. Production ingestion imports the corresponding normalized batch using the
   existing atomic importer, then `attach_batch_object` binds its ready object,
   policy and metadata. The batch must be validated. A published batch cannot gain
   extra artifacts or different artifact metadata; exact attachment retry is safe.
3. `publish_snapshot` builds the explicit union of the expected parent's batch IDs
   and new batch IDs. Each batch requires an artifact. The canonical manifest
   contains the exact dataset, snapshot, expected parent, complete sorted batch
   set and object/policy/source-metadata closure. It is staged and fsynced too.
4. The database locks the dataset, checks request identity and expected pointer,
   recomputes exact manifest equality, locks the complete object closure in sorted
   hash order, verifies ready states and current rights, then atomically publishes
   snapshot membership, permanent object references, the request record and pointer.
   Legacy synthetic publication is explicitly denied for a managed dataset, and
   runtime roles cannot execute the private legacy publication function.
5. Exact request retry returns its retained snapshot even after later pointer
   movement; changed request contents are rejected. A competing publisher must
   explicitly rebase under a new request identity. It cannot silently omit the
   winning publisher's batches.

Database runtime roles have no direct INSERT/UPDATE/DELETE rights on artifact
metadata. Narrow owner functions use fixed `pg_catalog` search paths and revoke
PUBLIC execution. Published policies, references and request records reject owner
mutation as well. Filesystem durability is established by trusted service code;
SQL cannot independently prove that the host called fsync.

## Reads, rights and retention

`require_snapshot_access` checks current rights for every contributing source
policy and verifies the complete retained object closure. `read_object` additionally
requires membership in that exact dataset/snapshot; it never falls back to current.
`read_staged_object` supports retained acquisition retries by complete ticket,
worker, policy and object identity. Revocation is append-only and immediately
blocks subsequent authorized-use checks. An allowed local analysis does not imply
export or model-input permission. Policy names are data, never permission to
execute source-document instructions.

`reader_pin` records the actual worker and snapshot. Scope exit releases a pin;
no timeout or TTL silently ends it. Published snapshots and their transitive
objects have **no automatic deletion**. `collect_abandoned` only considers registered
objects with no permanent reference and no active ticket, after the grace period
following explicit ticket completion/abandonment. The caller must establish the
associated worker finished or stopped before ending its ticket. A paused publisher
retains its active ticket regardless of age.

Deletion persists `deleting` before unlink, fsyncs the containing directory and
then persists a `deleted` tombstone. Either crash boundary can be retried. The same
bytes may later be registered again under a new active ticket. Unknown hash files
and orphan temporary files are never swept online: a separate quiesced operator
audit is required. This deliberately favors retention over unsafe cleanup.

## Verification

Artifact tests use real PostgreSQL roles and temporary filesystem roots, with a
separate database/root pair per test because content hashes are global objects.
They cover concurrent identical writers without inode replacement, a genuinely
blocked collector while a writer is paused after promotion, concurrent pointer
CAS, missing/corrupt/symlink bytes, exact manifest closure, denied legacy bypass,
current rights revocation, scoped reads, pinned readers, owner immutability,
four staging failure points, three publication failure points and two restartable
collection failure points. A child process actually exits immediately after
promotion; its committed active ticket prevents collection until process
termination has been observed and explicitly recorded by the test supervisor.

Independent evidence expectations and G1–G4 cases verify SQL selection answers
and concrete evidence mutations. These checks do not establish real-source
parsing quality, network availability, performance targets or external hosting.

## Request checks and content inspection

Snapshot access checks current rights, ready state and file metadata using anchored
directory descriptors and no symlink following. Report requests do not read object
content. Actual object reads, publication, `verify-objects` and backups stream full
SHA256 checks in 1 MiB chunks. Equal-size content corruption is detected by these
inspection boundaries, not by readiness or metadata-only report access. Published
object bytes remain service-owned and immutable by contract. Readiness checks only
policy/database/table availability independently of report admission capacity.
