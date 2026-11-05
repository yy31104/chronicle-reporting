# ADR 0004: Isolation and status presentation

Status: adopted.

Regression tests use same-name snapshots with different batch membership in separate datasets. Removing any dataset predicate must produce a detectable wrong answer.

Migration 003 requires READ COMMITTED for sealing. Other isolation levels fail before lookup and locking with `SEAL_REQUIRES_READ_COMMITTED`; the service never silently downgrades isolation.

Keep stored selection statuses. Without a source cutoff, display "No data in this snapshot". With a cutoff, use cutoff-specific wording. In report display, a source withdrawn or missing reason takes precedence over incompatible units; retain a unit mismatch warning and a null numerical value. [Hand-authored cases](../../tests/fixtures/reports/status-policy-v1.json) freeze these rules.
