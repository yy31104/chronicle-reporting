# ADR 0003: PostgreSQL roles and immutable snapshots

Status: adopted.

Use PostgreSQL 18 with explicit SQL transactions. Administrative setup, import, publication and report queries use separate roles.

Import admission checks immutable revision identity and retry fingerprints. Batch insertion and sealing share a row lock. Sealing verifies the declared occurrence count under READ COMMITTED.

Publication serializes on the dataset row, validates every member batch, compares the expected current pointer, and commits snapshot membership and pointer advancement together. An identical retry returns the original snapshot without moving the pointer backward.

Database roles are trusted local service roles. HTTP dataset scopes provide an additional access boundary; direct database credentials do not provide tenant isolation. Applied migration bytes remain immutable.
