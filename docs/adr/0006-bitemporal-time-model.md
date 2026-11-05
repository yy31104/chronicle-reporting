# ADR0006: Describe the time model in SQL:2011 bitemporal terms

Date: 2026-11-05. Status: **proposed**. This ADR describes behavior that is already
implemented by migrations 001–008, `sql/select_context.sql` and
`sql/select_evidence_v1.sql`; it changes no schema, query or code.

## Context

Chronicle stores macroeconomic revisions. The working contract already insists
that observation period, source availability and actual local knowledge are three
different things ([WORKING-CONTRACT](../WORKING-CONTRACT.md) §1–2). The
contract uses Chronicle-specific names (`observation_month`, `release_day`,
`received_at`, snapshot, mode). Reviewers who know temporal databases ask "is this
valid time or transaction time?", and the answer is not written down anywhere.

SQL:2011 standardized two kinds of time period, as summarized by Kulkarni and
Michels (2012):

- **Application-time period tables** (the literature's *valid time*). A period
  declared with `PERIOD FOR <name> (start_col, end_col)` over ordinary,
  user-maintained columns, describing when a fact holds in the modeled world.
  Users insert and change these values; SQL:2011 adds `UPDATE/DELETE … FOR PORTION
  OF` and `WITHOUT OVERLAPS` keys to manage them.
- **System-versioned tables** (the literature's *transaction time*). A period
  declared with `PERIOD FOR SYSTEM_TIME`, whose values are **assigned by the DBMS**
  when a transaction changes a row and cannot be set by users. Historical rows are
  retained, and `FOR SYSTEM_TIME AS OF t` (or `FROM … TO`, `BETWEEN … AND`) reads
  the table as the database recorded it.
- A table with both is a **bitemporal table**.
- Periods in SQL:2011 are **closed-open**: the start is included, the end is not.

## Decision

Use these terms, with the mapping below, when describing or extending Chronicle's
time model. Do not adopt SQL:2011 temporal syntax. Migrations 001–008 contain no
`PERIOD FOR`, `FOR SYSTEM_TIME`, `WITHOUT OVERLAPS` or `FOR PORTION OF` (checked with
`grep` on 2026-11-05). This ADR makes no claim about which of those features a
particular PostgreSQL release supports; none is relied upon.

Chronicle has **three** time axes, not two. SQL:2011 provides a built-in mechanism
for only the two it standardizes; the third is ordinary data.

| Axis | SQL:2011 term | Chronicle columns | Who assigns it | Period shape |
|---|---|---|---|---|
| Observation period | valid time / application time | `revision.observation_month` | Source (asserted in the publication) | closed-open `[month, next month)`, stored as the month start |
| Source availability | none built in: user-maintained, application-time-like | `source_release.release_day`, `availability_precision` (`date` \| `unknown`), `source_order`, `order_rule` | Source text, transcribed by the importer | `date`: the closed-open day `[release_day, release_day + 1)` with no intraday instant; `unknown`: no period at all |
| Chronicle knowledge | transaction time / system time | snapshot membership (`snapshot_batch`), `snapshot.created_at`, `revision_occurrence.received_at` | Chronicle | snapshots are immutable sets; see below |

### 1. Valid time is the observation period

A revision row says "for the period `[observation_month, next month)`, source
release *k* asserted value *v* (or `missing`, or `withdrawn`)". The end of the
period is implied by the month grain, so no end column is stored. Because valid
time here is a calendar grid, Chronicle never needs `FOR PORTION OF`. A revision
always covers exactly one whole month, and two revisions of the same month are
different assertions by different releases, not overlapping valid-time rows.

### 2. Source availability is the publisher's transaction time, stored as data

`release_day` is when the publisher made its assertion public: transaction time
**in the publisher's system**. In Chronicle it is not DBMS-assigned. It is
transcribed from publisher text (for example, "8:30 a.m. (ET) Friday, February 2,
2024" yields `release_day = 2024-02-02` with `date` precision). It therefore cannot
be SQL:2011 system time, which only the database itself may assign. It behaves like
a user-maintained, application-time-like attribute that happens to describe
somebody else's transaction time.

The `source_as_of` cutoff applies the closed-open convention to this axis.
`exclusive_end_date = d` admits releases with `release_day < d`. Date precision
deliberately does not establish an intraday instant: C1 rejects intraday cutoffs and
does not infer the 8:30 release clock from the text. `unknown` precision has no
period. Under correction F3, an unknown-availability revision whose source order
exceeds the eligible known winner yields `unknown_availability` with a null value,
rather than a guess.

### 3. Chronicle's knowledge is transaction time, committed by snapshot

Chronicle's own transaction time has two parts:

- **Snapshot membership is the commit unit.** A snapshot is an immutable set of
  sealed batches. Its `created_at` defaults to `statement_timestamp()`, so it is
  DBMS-assigned in the SQL:2011 sense. `reject_mutation` triggers make revisions,
  occurrences and snapshots append-only. No row ever gets a system-time end;
  supersession is expressed by a later source revision or a later snapshot, never
  by closing a period on an old row.
- **`received_at` records actual local receipt.** The acquisition pipeline supplies
  it (for example, the saved BLS archive receipt at 2026-10-29T18:00:24Z). It is
  local knowledge time, but it is not DBMS-assigned, and it is not publication time.

So `system_snapshot` mode is Chronicle's counterpart of
`FOR SYSTEM_TIME AS OF`, keyed by an explicit snapshot identity rather than a
timestamp. Every analysis context pins one snapshot, and a later dataset-pointer
move cannot change it. Chronicle offers no timestamp-keyed "as Chronicle knew at
instant t" query over `received_at`. Receipt times inside a snapshot are evidence,
not a commit order.

### Modes in these terms

| Mode | Valid time | Source availability | Chronicle transaction time |
|---|---|---|---|
| `latest_corrected` | requested months | no cutoff; greatest evidenced `source_order` | pinned snapshot |
| `source_as_of` | requested months | `release_day < exclusive_end_date`, plus the F3 unknown-availability block | pinned snapshot |
| `system_snapshot` | requested months | no cutoff; greatest `source_order` retained in the snapshot | pinned snapshot (the AS OF) |

`source_as_of` reconstructs **what the source had published** by a date. It does not
reconstruct what Chronicle knew on that date. The two real archives were published
in 2024 and received locally in 2026, so "source as of 2024-03-01" and "Chronicle
as of 2024-03-01" are different questions. Only the first is answerable here, and
the second would be empty.

### Worked example: January 2024 nonfarm payrolls

Retained evidence: [BLS cell audit](../../data/archive/releases.json).

- Valid time: `[2024-01-01, 2024-02-01)` for the change, using the levels for
  December 2023 and January 2024.
- Source availability: release `2024-02-02` (levels 157,347 and 157,700, so the change
  is +353 thousand) and release `2024-03-08` (157,304 and 157,533, so +229).
- Chronicle transaction time: both saved publications were received on 2026-10-29,
  and they are pinned by snapshots `BLS-2024-02-02` and `BLS-2024-03-08`.
- By the selection rule, a `source_as_of` context with `exclusive_end_date =
  2024-03-08` admits only the February release, because `2024-03-08 < 2024-03-08` is
  false, and reports 353. A cutoff of `2024-03-09` admits both and selects the
  greater source order, 229. These two lines are derived from the rule; this ADR
  did not execute them as a new query.

### Status vocabulary is orthogonal to time

`missing` and `withdrawn` are source assertions about a valid-time period.
`not_available_at_cutoff` means no admissible release exists on the
source-availability axis. `unknown_availability` means the source-availability
value itself is unknown. These four stay distinct, and none becomes zero.

## Consequences

- New code and documents should say *valid time (observation period)*, *source
  availability*, and *transaction time (snapshot / receipt)*. They should avoid
  "as of" without saying which axis it refers to.
- A future intraday extension would add evidenced source instants with a time zone
  to the source-availability axis. It would not turn that axis into SQL:2011 system
  time.
- A future timestamp-keyed Chronicle-knowledge query would need a DBMS-assigned
  commit time per snapshot. `snapshot.created_at` exists, but no contract or query
  uses it as an AS OF key today; adding one is a separate decision.
- Adopting SQL:2011 or PostgreSQL temporal syntax would need a new migration and
  its own ADR. Migrations 001–008 must not be edited.

## References

- Kulkarni, K., & Michels, J.-E. (2012). Temporal features in SQL:2011. *ACM SIGMOD
  Record*, 41(3), 34–43.
- [WORKING-CONTRACT](../WORKING-CONTRACT.md), [ADR0002](0002-c1-contracts.md),
  [ADR0003](0003-postgres-snapshots.md), `src/chronicle/sql/001_schema.sql`,
  `src/chronicle/sql/select_context.sql`, `src/chronicle/sql/select_evidence_v1.sql`.
