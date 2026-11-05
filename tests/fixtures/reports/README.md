# PR03 report-status expectations

`status-policy-v1.json` freezes five hand-authored report-display expectations
with independent expected output for two presentation rules:

- Preserve existing stored statuses; no-cutoff absence displays “No data in this snapshot”.
- Source withdrawal/missing is the primary report reason, with unit mismatch kept
  as an additional diagnostic.

`storage_status` is the current selector's compatibility result. `source_status`
is the selected revision's original state, obtained by PR03's future evidence path.
`display_status` and `message_code` drive presentation only. The report must not
use a display choice to turn a null result into a numeric input.

These records do not change G1–G4, the existing oracle or the minimal production
selection output. They are acceptance inputs for PR03, not evidence of an executed
report renderer. Their SHA256SUMS protects accidental edits; a deliberate policy
change needs reviewed new expectations before implementation.
