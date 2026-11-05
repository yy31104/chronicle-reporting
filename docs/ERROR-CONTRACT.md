# Stable delivery errors

Version: `chronicle.errors.v1`, 2026-10-29.

The Python storage boundary translates only registered PostgreSQL diagnostics.
It matches SQLSTATE, schema, table and constraint identity; localized message text
is never inspected. Migration `004_structured_errors.sql` renames ambiguous
auto-generated constraints and adds structured diagnostics to existing protected
functions. Applied migrations 001–003 remain unchanged. Function behavior, owner,
security-definer boundary, grants and SQLSTATE classes are retained.

`database_errors()` wraps outside `connection.transaction()`, allowing rollback
before a typed exception escapes. The context manager is also available to future
storage operations. A nested transaction only rolls back its own savepoint;
callers controlling an outer transaction still own that outer transaction.

## Categories

| Condition | Internal result | Public code / retryable |
|---|---|---|
| Distinct releases share a source order, within one incoming batch or against committed data | `ContractError` | `AMBIGUOUS_SOURCE_ORDER` / false |
| Equal revision identity has different content | `ContractError` | `REVISION_CONFLICT` / false |
| Equal release identity has different release evidence | `ContractError` | `RELEASE_CONFLICT` / false |
| Known CHECK or foreign-key rejection | `ContractError` with registered code | Same code / false |
| Known publication/context/closure rejection | `ContractError` with registered code | Same code / false |
| SQLSTATE 40001 | `RetryableTransactionError`, reason `SERIALIZATION_FAILURE` | `TRANSACTION_RETRY_REQUIRED` / true |
| SQLSTATE 40P01 | `RetryableTransactionError`, reason `DEADLOCK_DETECTED` | `TRANSACTION_RETRY_REQUIRED` / true |
| Unknown database constraint/error | Original PostgreSQL exception, for trusted diagnostics | `DATABASE_ERROR` / false |
| Other unexpected exception | Original exception | `INTERNAL_ERROR` / false |

The closed mapping in `src/chronicle/errors.py` is the code-level registry. Known
input CHECK codes include `INVALID_IDENTITY`, `INVALID_ORDER_RULE`,
`UNSUPPORTED_PRECISION`, `INVALID_AVAILABILITY`, `INVALID_SOURCE_ORDER`,
`INVALID_MONTH`, `INVALID_STATUS`, `INVALID_UNIT`, `INVALID_REVISION_VALUE`,
`INVALID_MANIFEST_HASH`, `INVALID_OCCURRENCE_COUNT` and `INVALID_RECEIPT`.
The combined revision status/value constraint maps to `INVALID_REVISION_VALUE`;
its text is not parsed to guess which constituent predicate failed. Typed input
validation can reject an invalid decimal earlier with a more specific code.

Runtime database permissions are not input validation. Permission failures and
unknown schema/programming failures remain infrastructure failures. An unrecognized
unique violation is never automatically an ambiguous source order. The same
constraint name under another schema or table is also unrecognized.

## Retry and publication

There are no automatic retries. A retryable failure is not a persisted rejection
and never means that a partial transaction should resume. A caller may retry the
whole operation with the original batch, context, snapshot or report identity.
Do not retry only the final failed SQL statement. Timeouts and connection failures
are not asserted to have an automatically safe retry policy by this contract.

CLI/API delivery must use `public_error(exception)` instead of `str(exception)` or
tracebacks. Its envelope has `code`, `message` and `retryable`. Messages are static;
SQL, connection credentials, raw constraint details and data values do not appear.
For typed contract errors, even their internally supplied message is discarded.
Request correlation/logging belongs to the delivery service, not these messages.

## Verification

The real-server tests in `tests/postgres/test_errors.py` cover same/cross-batch
ambiguity and rollback, known CHECK and foreign-key failures, content conflicts,
localized function-message independence, spoofed schema/table/constraint names,
unknown database errors and safe rendering. Two actual concurrent transaction
histories produce PostgreSQL serialization failure and deadlock; the victim's
writes roll back and the retryable class remains distinct from input rejection.

On 2026-10-29 the command below passed **42 tests** (16 error tests plus the
existing 10 transaction and 16 permission tests) in disposable local PostgreSQL
databases. This is verification of this slice, not an assertion that all later
report or production deployment work is complete.

```text
UV_CACHE_DIR=.cache/uv UV_OFFLINE=1 uv run --locked pytest --tb=short \
  tests/postgres/test_errors.py tests/postgres/test_transactions.py \
  tests/postgres/test_permissions.py
42 passed in 2.78s
```
