# ADR0007: Deliver the exact revision difference beside the stored-output difference

Date: 2026-11-05. Status: implemented.

## Context

`chronicle.comparison.compare_reports` subtracts two retained canonical outputs,
each already rounded to 12 places. `chronicle.exact_revision` adds a second
difference: the exact rational difference of the retained inputs, rounded once.
This ADR makes that difference available in the API, the UI and the CLI.

## Decision

- **API.** Add `POST /v1/datasets/{dataset_id}/exact-comparisons`. Its body is the
  same as `/comparisons` (`old_report_id`, `new_report_id`). It returns schema
  `chronicle.report-comparison-exact.v1`, which embeds the unchanged
  `/comparisons` document under `stored_comparison`, plus per-row
  `stored_output_difference`, `exact_difference`, `exact_difference_rational`
  (string numerator/denominator) and `rounding_gap`
  (`stored_output_difference − exact_difference`). Before computing, the endpoint
  applies the same checks to both operands as `/comparisons`: dataset scope and
  current `local_analysis` rights. `/comparisons` itself is unchanged.
- **CLI.** `chronicle compare OLD NEW --exact` prints the exact document. Without
  `--exact`, the output is unchanged.
- **UI.** The comparison table now calls `/exact-comparisons`. Next to the
  existing "Difference（Saved output subtraction）" column it shows "Exact difference（Rational subtraction, rounded once）".
  When the exact value is not an integer, the cell shows its rational form. When
  the two differences disagree, it shows the gap. Unavailable or unit-mismatched
  rows keep the existing "Not comparable"/"Incompatible units" text in both columns, never a zero.
- **Errors.** Two new contract codes are used: `STORED_COMPARISON_MISMATCH` and
  `EXACT_RECOMPUTATION_MISMATCH`. Like other non-specific contract violations,
  they map to HTTP 422 through the existing default branch. Existing codes
  (`REPORT_NOT_FOUND` 404, `RIGHTS_DENIED` 403, `REPORT_CORRUPT` 422,
  `INCOMPATIBLE_CONTEXT` 422) pass through unchanged.

## Consequences

- The UI now depends on the new endpoint. A deployed image built before this change
  does not serve it, so the private deployment must be rebuilt before the UI works.
  No migration is needed, and the computation creates no database rows.
- Report implementation identity (`reports.implementation_identity`) is unchanged,
  because `reports.py`, `numeric.py` and the SQL files were not edited.
  `comparison.py` is also unchanged, so four-run comparison identities are not
  affected.
- The four-run `/matrix` attribution still subtracts stored outputs. Extending it
  is a separate decision.

Verification evidence is in
[the synthetic browser check](../../tests/browser_exact_difference.cjs).
