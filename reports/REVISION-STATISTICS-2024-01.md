# Revision statistics: first release vs later vintages (January 2024)

Generated data: [`revision-statistics-2024-01.json`](revision-statistics-2024-01.json),
produced by `scripts/revision_statistics_report.py` from attributed cells in
`data/archive/releases.json`. The regeneration check
(`tests/test_revision_report.py`) fails if the JSON no longer matches its inputs.
This is a descriptive report over **retained** vintages only. It makes no inferential
claim.

## Method (after Aruoba, 2008)

Aruoba (2008) treats a revision as a later estimate minus the initial announcement.
He asks whether revisions behave like rational forecast errors: zero mean, small
variance, unpredictable. This report computes the descriptive part, per series:

- **Revision** `r(m, h) = value in the h-th later retained vintage − first release`,
  for each observation month `m` whose first release is retained. `latest` uses the
  last retained vintage, which is the "final" proxy within this archive.
- **Statistics per horizon:** `n`, mean, sample standard deviation (`n − 1`
  denominator), and direction counts (up / down / unchanged).
- **First release:** the vintage whose reference month equals `m`. For BLS this is
  cross-checked against the release title "2024 M01 Results". Months that appear
  only as already-revised values (for example December 2023, first published on
  2024-01-05, which is not retained) are listed and excluded. They are not
  relabeled as first releases.
- **Arithmetic:** exact rationals from lexical decimals. Each published statistic
  is rounded once (12 places, half-even). The standard deviation is the once-rounded
  square root of the exact variance. It is reported as `null`, never `0`, when
  `n < 2`. Missing or withdrawn values are counted as unavailable and never become
  zero.
- **Time axes:** vintage date is the publisher's release date (source availability,
  date precision). Chronicle actually received these publications on 2026-10-29.
  Nothing here describes what anyone knew intraday.

## Headline example: January 2024 nonfarm payrolls, 353 → 229

| Vintage | Release | Dec 2023 level | Jan 2024 level | Jan 2024 change |
|---|---|---|---|---|
| 2024-02-02 | USDL-24-0148 | 157,347 | 157,700 | **+353** |
| 2024-03-08 | USDL-24-0451 | 157,304 | 157,533 | **+229** |

Units are thousand persons, seasonally adjusted, from Table B-1 total nonfarm. The
first-release revision to the January change is **−124 thousand (downward)**. That
matches the publisher's own narrative ("124,000, from +353,000 to +229,000"), as
recorded in the [cell audit](../data/archive/releases.json).

## Statistics by series

All four series have one revisable first release (January 2024) and two retained
vintages, so horizon 1 and `latest` coincide.

| Series | Unit | n | Mean revision | Sample SD | Direction (up/down/unchanged) |
|---|---|---|---|---|---|
| BLS total nonfarm, monthly change | thousand persons | 1 | −124.000000000000 | undefined (n<2) | 0 / 1 / 0 |
| BLS total nonfarm, level | thousand persons | 1 | −167.000000000000 | undefined (n<2) | 0 / 1 / 0 |
| FRB G.17 industrial production index | 2017=100, SA | 1 | −0.400000000000 | undefined (n<2) | 0 / 1 / 0 |
| FRB G.17 three-month mean (`production_mean3`) | 2017=100, SA | 1 | −0.033333333333 | undefined (n<2) | 0 / 1 / 0 |

Excluded from the statistics, with the reason recorded in the JSON:

- **First release not retained:** BLS level December 2023; FRB index November and
  December 2023.
- **No later retained vintage:** February 2024, in every series.

The FRB three-month-mean revision is exactly −1/30. Rounded once, that is
**−0.033333333333**. Subtracting the two rounded published means
(102.633333333333 − 102.666666666667) would give −0.033333333334, because it rounds
twice. This report uses the once-rounded exact value.

## What these numbers do and do not show

- Every retained first release was revised **down** in the next vintage. With
  `n = 1` per series, this is an observation, not evidence of bias. Aruoba's tests
  (mean-zero tests, noise-to-signal ratios, predictability regressions) need long
  vintage histories and were **not** computed.
- The January Employment Situation release (here 2024-02-02) is customarily the one
  that incorporates the annual CES benchmark revision. That was not checked against
  the retained HTML for this report, and benchmark and sample-based revisions are
  not separated here.
- Horizons count retained vintages, not publisher releases. Both archives here hold
  consecutive monthly releases, so horizon 1 is the second estimate.
- No new data was acquired. Extending the sample needs more retained vintages
  through the existing scoped-rights acquisition path. That was not done here.

## Reference

Aruoba, S. B. (2008). Data revisions are not well behaved. *Journal of Money,
Credit and Banking*, 40(2–3), 319–340.
