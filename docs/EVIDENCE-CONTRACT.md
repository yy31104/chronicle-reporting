# Evidence schema v1

Implemented 2026-10-29. `chronicle.evidence.select_evidence(connection, context_id)`
returns the immutable context's requested grid in its stored order. It is a
separate projection from the compatible C1 minimal selector. The schema name is
`chronicle.evidence.v1`.

Every slot identifies dataset, snapshot, namespace, series, month, mode and the
exclusive source cutoff, if present. `status` remains the legacy storage status;
`source_status` and `unit_compatible` independently expose the selected source's
state and compatibility with the requested unit. A missing expected unit means
no requested-unit restriction, not proof that all report inputs share a unit.
The report must separately compare units across its contributors.

Only top-level `value` is authoritative numeric input. A selected revision retains
its lexical value, source order, availability precision/date, unit metadata and
complete composite identity. For blocked source-as-of queries, selected revision,
source status, unit compatibility and authoritative value are null. The highest
order membership-restricted unknown revision is retained as `blocking_revision`,
which is uncertainty evidence and must not become a numeric input. An absent slot
has neither selected nor blocking revision.

Occurrences contain the full revision key, dataset/snapshot, batch ID and receipt
instant. They come from the same relation that admits a revision into this exact
snapshot, never from all receipts attached to its global identity. Every matching receipt is returned when the request fits the service limits
below, sorted by batch identifier in PostgreSQL C collation and then receipt time. UTC timestamps always contain six fractional second digits and Z;
they do not depend on the reader's session time zone. Date-only source evidence
never gains an inferred time of day. Synthetic fixture clocks remain synthetic.

The independent frozen evidence corpus precedes the production query and includes
reobservation, unknown blockers, outside-snapshot unknowns, source-state/unit
conflicts, absent slots and inverted source-order/date cases. All original G1–G4
outputs remain unchanged. Real-database tests independently remove each of the
four dataset scope conditions, the snapshot condition and the unknown blocking
comparison, and check concrete incorrect values or source evidence rather than
accepting SQL errors as mutation detection.


## Local service resource limits

`MAX_OCCURRENCES_PER_REVISION = 100` applies independently to the selected or
blocking revision in each returned slot. SQL applies all dataset, snapshot and
revision-identity predicates **before** limiting the receipt subquery to 101
rows. That limit is inside the input to `jsonb_agg`, so PostgreSQL does not first
allocate an unbounded receipt JSON array. The extra row detects overflow; the
application raises `EVIDENCE_TOO_LARGE` rather than returning the first 100.
Foreign datasets and batches outside the pinned snapshot do not consume the cap.
An unselected older revision does not change the returned evidence limit.

The complete canonical UTF-8 JSON evidence array is capped at **4 MiB**, including
array brackets and separators. A named PostgreSQL server cursor fetches one slot
at a time, so the client does not buffer the whole result before enforcing that
limit. If either cap is exceeded, the entire request fails without returning a
partial grid or partial receipt list. Existing immutable sources and scalar
selection remain intact. These bounds belong to the local service contract and
do not justify silently dropping evidence or treating a missing input as zero.

After this change the actual command was:

```sh
UV_OFFLINE=1 UV_CACHE_DIR=.cache/uv uv run --locked pytest --tb=short tests/postgres/test_evidence.py tests/postgres/test_reports.py tests/postgres/test_job_pins.py -q
```

Result: **64 passed in 5.27 seconds**, with one existing Starlette/httpx deprecation
warning. New real-database checks accept all 100 receipts in order; reject 101 for
both a selected revision and a blocking unknown; exclude 101 foreign receipts and
an outside-snapshot receipt from the cap; inspect the SQL result for a 450-receipt
revision and establish that only the 101-row overflow probe was aggregated; and
reject an 18-month, individually valid evidence grid at the actual 4 MiB limit.
The named cursor is closed after rejection. All four dataset-scope mutations,
legacy G1–G4 equivalence, retained report tests and actual HTTP job pin/cancellation
regression remain passing.
