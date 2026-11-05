# Frozen evidence corpus

`evidence-v1.json` was hand-authored on 2026-10-29 before the production
evidence query. It contains eight literal expected query results. `$dataset` is
a placeholder for the isolated test dataset identity only. No production output
was used to generate the values, selected revisions, matching occurrences,
unknown blockers, unit compatibility or source-status expectations.

Coverage includes retained lexical precision, reobservation restricted to pinned
batches, higher unknown availability, an unknown outside the snapshot, absence,
withdrawn/missing with unit mismatch, and source-order/date inversion. Times are
synthetic and use canonical UTC with six fractional second digits.

SHA256SUMS freezes these records. Existing G1–G4 remain unchanged. The independent
raw-dictionary evidence scan in tests/reference/evidence_model.py has no
production imports and is checked against these literals before SQL comparison.
