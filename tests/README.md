# Tests

`make test` runs the pure tests and explicitly deselects PostgreSQL tests.
`make test-postgres` runs the PostgreSQL tests against a real PostgreSQL 18 server,
using distinct non-admin runtime connections. The pytest summary line gives the
current counts. Missing server/access is a failure, not a skipped success. Use
`make dev-up db-migrate` first.

- `test_contracts.py`: production typed inputs and immutable revision admission.
- `test_numeric.py`: production lexical parsing/exact rounding versus literal answers
  and independent Fraction arithmetic.
- `test_reference.py`: frozen G1–G4 versus raw full scan and Fraction metric models.
- `postgres/test_queries.py`: actual SQL versus both literal answers and raw scan,
  grid/namespace/series/dataset isolation, and SQL F3 guard mutation.
- `postgres/test_permissions.py`: runtime role/definer privileges, immutable DML
  denial, staged-publication rejection and closed-batch append rejection.
- `postgres/test_constraints.py`: direct SQL bypasses Python to test lexical, date,
  scope, mode, source-order and batch-completeness constraints.
- `postgres/test_transactions.py`: atomic conflict rollback/reobservation, retry
  binding, pointer CAS, context pins and real concurrent sessions.
- `postgres/test_isolation.py`: READ COMMITTED seal/count behavior and rejection
  of unsupported levels before lookup/locking; original REPEATABLE READ overfull
  seal reproduced only in a separately disposable test database.

The suite creates a unique `chronicle_test_*` database and removes that database in
cleanup. Per-test datasets are unique. It does not reset other projects or the
local `chronicle` development database. Concurrent schedule tests verify real lock
waits with `pg_blocking_pids`; a sleep alone is not evidence of the desired ordering.

Golden fixture bytes and expected answers remain frozen; their SHA256SUMS detect
accidental edits. Reference selection/formulas do not import production helpers.
Both the reference and actual SQL unknown-time mutants produce the wrong old 100,
which fails the unchanged unknown/null assertion. Mutation runs restore normal SQL.

The dataset-scope regressions use identical snapshot IDs with different batch
membership. Three independent selector mutations remove the outer dataset filter,
membership-to-occurrence scope and occurrence-to-revision scope. The latter two
then return January 103 instead of the old snapshot's frozen 100. The former returns
deterministic foreign 999 values rather than depending on SQL tie-breaking. These
are selector sensitivities; future PR03 evidence/report SQL needs its own mutations.

The separate `fixtures/reports/status-policy-v1.json` is a hand-authored acceptance
corpus for the user-approved PR03 display policy. It does not change G1–G4 or claim
an implemented/passing renderer.

`make demo-reference` is pure reference-only. `make demo-postgres` executes actual
SQL over the same 20 synthetic cases and retains their contexts in the development
DB. Neither is a fixed report/API, real data, generated answer quality or performance benchmark.
All synthetic receipt clocks are clearly labeled; no personal mastery is inferred.
