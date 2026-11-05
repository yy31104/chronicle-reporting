# C3: controlled comparison, incremental arithmetic and reproducibility

These local service capabilities retain source-rights and API identity checks.
The correctness checks below do not establish performance under production load.

## Canonical comparison and controlled executions

`comparison.compare_reports(connection, old_report_id, new_report_id)` subtracts
the retained canonical decimal strings using exact scaled integers. Mode, cutoff,
source namespace, series, output grid, expected unit, formula, numeric policy and
unit policy must match. Missing or incompatible cells never become zero.

`comparison.compare_matrix(connection, old_context_id, new_context_id, formula,
comparison_id=..., output_months=..., old_code=..., new_code=...)` retains an atomic
four-run record. The service derives all four run IDs from the comparison ID;
callers cannot provide their own results or child identities. Two base reports
pin the old/new data contexts and their calendar halos. Both code versions execute
against both complete evidence sets under identical controls. The record includes
every child output, input evidence, code label, current runtime/source identity
and the exact comparison request. Identity retries return the saved record;
different parameters under that ID fail. `replay_comparison` verifies its digest.

The finite shipped registry contains:

| Code version | Actual executable behavior | Scope |
|---|---|---|
| `fixed-v1` | The fixed employment difference / three-calendar-month mean | All permitted input namespaces |
| `synthetic-double-v1` | Double the exact formula before its single canonical rounding | Only the explicitly named `synthetic` namespace |

The second version demonstrates a real arithmetic code change and interaction;
it is not a replacement economic definition or a claim to recover a prior deployed
binary. Both branches are concrete code shipped in the current package. Unknown
versions return `ENVIRONMENT_UNAVAILABLE`; arbitrary code execution is unavailable.
Use `fixed-v1` on both axes for real-data comparisons until another reviewed,
versioned economic formula exists.

For canonical outputs A=old-data/old-code, B=new-data/old-code,
C=old-data/new-code and D=new-data/new-code:

```text
data_effect = B - A
code_effect = C - A
interaction = D - B - C + A
total_change = D - A
```

The example 10/12/20/24 has effects 2, 10, 2 and total 14. The G1 database case
10/13/20/26 has effects 3, 10, 3 and total 16. A changed cutoff or mode is incompatible, rather than mislabeled data-only attribution.
Unavailable child values produce an incomplete result; units must agree for all
numeric terms. Range failure aborts the full comparison and its newly built bases.
These are controlled arithmetic attributions, not causal explanations of economics.

## Incremental calculation

`incremental.build_incremental_report(connection, old_report_id, new_context_id,
report_id=...)` retains a new immutable report. The formula's static footprint
includes the previous calendar month for employment and the two preceding months
for production. Absent slots are dependencies, so late backfills and withdrawals
invalidate their dependent windows. Changing an input month affects it and the next
one or two requested output months, including lookbacks outside the output grid.

The builder reads every input from the new pinned snapshot and rebuilds all output
evidence and display states. It reuses a prior canonical numeric value only if the
entire effective dependency selection is unchanged. Receipt-only changes refresh
provenance but need not repeat arithmetic. Source revision, availability, state,
lexical value, unit and blocking uncertainty changes invalidate arithmetic. Changes
to implementation identity, schema, expected-unit policy or unit metadata force a
documented full recomputation. The old report and new context must belong to the
same dataset; identical input values in another dataset do not permit reuse.
Dataset/mode/cutoff/series changes reject as incompatible and roll back the new
report and any newly created lookback context.

`execution` binds the previous report and algorithm to the new request identity.
`execution_result` records strategy, fallback reason, changed input months and
recomputed/reused output months. Full reexecution uses the complete calculation,
so it can independently detect a stale incremental result. Database reads are
currently complete; this optimization reduces formula evaluations only. No speedup
claim is made without a benchmark on the actual workload.

## Reproducibility bundles and rights

`bundles.export_report_bundle(connection, report_id)` checks the current snapshot
policies for `public_export`, including live revocation, before returning archive
bytes. A local-analysis grant alone does not permit this portable export. The
legacy synthetic fixtures have no external source policy. The API must additionally
check the requesting identity for the report's dataset.

Archives contain the retained report and exact selected input evidence, a content
manifest and, only when available, matching package source files, full pinned
`uv.lock`, exact `pyproject.toml`, packaged README snapshot and runtime identity.
The Python version file is explicit. They exclude credentials, raw unrelated
objects, private paths and caches. Source inputs here are the retained parsed
revisions and receipts; raw-source archival closure remains the object subsystem's
separate responsibility. If the recorded executable/runtime identity or exact lock
is unavailable, the manifest says so and retains identity replay without substituting
the current environment. The package carries `runtime.lock` and its hash, plus
`runtime-pyproject.toml`; dependency/project changes must update these from the
authoritative `uv.lock` and `pyproject.toml`.

Dependency wheels are optional and all-or-nothing. When `CHRONICLE_WHEELHOUSE`
names a directory (`make wheelhouse` fills `var/wheelhouse` with hash-checked
wheels for the current platform and Python 3.12), the exporter adds every wheel
whose SHA-256 appears in the packaged `runtime-requirements.lock` under
`environment/wheels/`, plus `environment/OFFLINE-INSTALL.md`. If any requirement
whose marker applies on this platform has no matching wheel, no wheels are added.
The manifest records `dependency_wheels` (`included` or `not_included`), the
reason when not included, and the platform the wheels were collected for. With
wheels, `uv pip install --offline --no-index --find-links wheels --require-hashes`
installs the dependencies with an empty package cache; without them, installation
needs network access or a cache that already holds the locked wheels. The package
carries `runtime-requirements.lock`, which must equal `requirements.lock`; it is
not part of the recorded implementation identity, so adding it changes no
retained report identity.

`verify_bundle(bytes)` never extracts or executes archive members. It checks bounded
expanded size and member count, safe normalized paths, no duplicates/symlinks,
complete manifest coverage, all file hashes, input closure, and recorded source/lock
identity. Wheels must be declared in the manifest and each must match a hash in the
packaged hash-pinned requirements. Verification establishes content integrity, not a publisher signature,
installation of dependencies, or successful code execution. Replaying the report
and executing the historical environment remain distinct operations.

## Verification scope

Tests include the independent literal matrix, canonical last-place subtraction,
SQL-backed child runs and identity binding, whole-transaction incompatibility
rollback, owner immutability, independent calendar-footprint scans, backfill,
withdrawal and metadata fallback, a seeded 12-revision PostgreSQL trace against
full computation and independent Fraction arithmetic, and a deliberately broken
invalidation function that produces observable stale values. Bundle tests cover
determinism, changed payload, unavailable environment, path/size constraints and
live export-policy revocation. Execution counts and commands belong to the release
evidence record, not to this contract.
