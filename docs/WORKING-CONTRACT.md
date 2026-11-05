# Chronicle working contract amendment

Version: `chronicle.working.c1.v1`, 2026-10-23.

This contract defines the time, identity, unit and numeric rules used by the application and its independent reference tests. See [database boundaries](POSTGRES-CONTRACT.md) for persistence and [time model](adr/0006-bitemporal-time-model.md) for the three separate time coordinates.

## 1. C1 input and time boundary

Observation months are calendar month starts. Source availability, actual local
receipt and snapshot membership are separate fields with separate meanings.
Local receipt instants must be timezone-aware and are normalized to UTC. Synthetic
receipt times and snapshots are explicitly synthetic; their dates are not actual
installation history.

Every current pure input context supplies an explicit snapshot ID. There is no
implicit current snapshot lookup in C1-PR01. A later context service may resolve
the current dataset pointer exactly once and persist its chosen ID; later pointer
changes must not alter that context.

| Mode | Required time input | Selection meaning within the supplied snapshot |
|---|---|---|
| `latest_corrected` | No source cutoff | Greatest evidenced source order; unknown availability stays visible as a warning |
| `source_as_of` | Date cutoff with `exclusive_end_date` | Reconstructed source knowledge, subject to the uncertainty rule below |
| `system_snapshot` | No source cutoff | Greatest evidenced source order actually retained in this snapshot |

`exclusive_end_date=2026-03-16` includes known source release dates strictly before
March 16, including March 15. It is a calendar-date rule, not midnight UTC.
C1 accepts source availability at date or unknown precision and rejects instant
source-availability inputs and **all intraday cutoff requests**. A future extension
may retain evidenced source instants together with their source date and time zone;
that does not automatically enable intraday queries. C1 does not infer a release
clock from a date or upgrade source-time reconstruction into local historical
knowledge.

### Status presentation

Keep existing selector statuses. For `not_available_at_cutoff` with no source
cutoff, report the mode-aware message “No data in this snapshot”. Source-as-of absence uses
cutoff-specific wording. For a source `withdrawn`/`missing` record with incompatible
units/metadata, prioritize that original source reason in the report and preserve
`unit_mismatch` as a warning; do not change the selector's legacy status or null
value. PR03 must obtain the original source status through its evidence path.
The [hand-authored examples](../tests/fixtures/reports/status-policy-v1.json) precede
renderer implementation; they do not establish a completed report feature.

## 2. Revision identity and source-as-of uncertainty — F3

The natural key is `(source_namespace, series_key, observation_month,
source_revision_key)`. Identical content under the same key may add a provenance
occurrence; conflicting content is rejected for quarantine by a future importer.
Receipt time, row order, UUID and content hash cannot invent source order.
Distinct same-day revisions require an evidenced ordering rule. An unresolved
ordering conflict cannot enter accepted snapshot inputs.

Within one source namespace all accepted releases use the same order rule.
`date_ordinal` must equal the evidenced date's ordinal; distinct releases on the
same day cannot both use that rule. `documented_sequence` requires adapter-supplied
comparable ordering evidence. Mixing those coordinate systems, even on different
days, is rejected. These types do not independently prove an adapter's evidence.

`documented_sequence` currently does not enforce monotonic release dates. A higher
order can have an earlier known availability date. C1 first filters by the source
cutoff, then chooses maximum eligible source order; it does not choose maximum
release date or independently prove source chronology. The F3 rule uses the supplied
precedence of unknown-time revisions conservatively within snapshot membership.
Before C2 real-source ingestion, the adapter must establish and test the meaning
and availability relationship of its evidenced order, or adopt a justified
monotonic known-date validation policy. Inverted known dates and unknown dates need
explicit cases. This remains a real-source acceptance gate, not an assumption that
every accepted integer already proves chronology. PR03 records the existing
inverted-date behavior before any versioned policy change.

For each requested series and month, the scan oracle and future production query
apply these rules in order:

1. Restrict revisions to the explicit snapshot membership and requested series/
   month. Retain both known and unknown availability in this restricted set.
2. For `source_as_of`, find the greatest-order known revision whose evidenced
   release date is earlier than `exclusive_end_date`.
3. If any membership-restricted unknown-time revision has greater source order
   than that candidate, or no eligible candidate exists and any such unknown-time
   revision exists, return `unknown_availability` with a null authoritative value.
4. Otherwise return the known candidate's actual state and value. With no eligible
   candidate and no unknown-time revision, return `not_available_at_cutoff`.

An older unknown revision below an eligible newer known winner does not block
that winner. An unknown revision outside the snapshot has no effect. These are
separate required tests. A blocked older candidate may be presented only as
incomplete evidence, never as an authoritative value. The current oracle need not
implement that optional presentation.

`missing`, `withdrawn`, `not_available_at_cutoff` and `unknown_availability` remain
distinct. Winning withdrawal or explicit missing records never fall back to an
older number. Every requested month survives with either a value or a status.
Unit and metadata compatibility is checked before arithmetic; a thousand-person
series and a person series are not silently interchangeable.

## 3. Exact numerical policy — F2

Retain the original lexical source decimal and its unit. Parse accepted decimal
strings exactly into an integer at scale `10^12`; do not route source values
through binary floating point or a rounding Decimal context. The grammar is plain
ASCII `[+-]?[0-9]+(?:\.[0-9]+)?`, with a maximum of 128 characters before
conversion. A leading plus and leading zeroes are permitted. Whitespace,
exponents, grouping, a missing integer part and a trailing decimal point are not.
Source values with more than 12 fractional digits are rejected even if the excess
digits are zero. Unsupported syntax is rejected, not silently normalized. Source
adapters needing another grammar must preserve the raw text and make an explicit,
reviewed conversion.

The compatible numeric domain is `abs(scaled_integer) <= 10^38 - 1`, corresponding
to at most 26 integer digits and 12 fractional digits. This storage boundary does
not by itself declare every value meaningful for a particular economic series;
series-specific domain rules remain separately versioned. Canonical output is a
decimal string with exactly 12 fractional digits, no exponent or grouping, and
no negative zero. Twelve places describe arithmetic representation, not measured
source precision.

For the supported fixed formulas, retain exact integer numerator and denominator
until canonical output. Normalize the denominator to positive. Divide the absolute
scaled numerator into quotient and remainder. Increment the quotient when twice
the remainder exceeds the denominator, or equals it and the quotient is odd;
then restore the sign. Check both the exact output ratio before rounding and the
canonical output against the numeric range; a ratio just outside the domain is
rejected even if it would round back inside. Publish no partial result on overflow.
Division by zero is an explicit failure. No intermediate fixed precision rounding
is permitted. Python arbitrary-size integer intermediates avoid rounding; they do
not expand the accepted source/output domain.

The current primitives are `parse_scaled(str) -> int`, `format_scaled(int) -> str`
and `canonical_ratio(int, int = 1) -> str`; the ratio inputs represent a real-unit
numerator and denominator, not a pre-scaled output. Malformed source syntax or
non-string source input yields `INVALID_DECIMAL`; exceeding lexical length, scale,
numeric input/output bounds yields `NUMERIC_OUT_OF_RANGE`; a zero denominator
yields `ZERO_DENOMINATOR`.

Employment differences are exact scaled subtraction. A complete three-month mean
uses exactly those three calendar months and does not skip missing values. The
optional year-over-year report remains later work; establishing or testing its
exact numerical primitive does not deliver that report feature. A difference
between existing report outputs subtracts their **stored canonical values**.
It does not re-round a difference of hidden unrounded results.

Preserve the F2 regression input and expected output:

```text
x = 20000000000000100000000000.000000000003
y = 200.000000000001
formula = 100 * (x / y - 1)
correct canonical output = 9999999999999999999999900.000000000001
```

The independent arithmetic oracle uses Fraction and its own rounding logic. It
must not import the production parser, rounding, formula or selection helpers.
Required checks include the F2 example, exact ties and both neighboring sides,
negative signs, zero denominators, input/output range boundaries and incompatible
units. A matching implementation/oracle answer alone is insufficient: hand-authored
expected strings anchor the comparison.

## 4. Typed fact rendering

The deterministic fact renderer accepts typed assertions that name a report, month, value, unit and evidence references. It validates exact agreement with the retained report and permitted snapshot before rendering fixed factual templates. Unsupported assertion kinds and unsupported premises fail closed. A valid citation cannot excuse a wrong value, period, status or unit.

## 5. Independent verification

Hand-authored G1-G4 inputs and expected outputs anchor full-scan and exact-rational reference models. Database tests verify SQL equivalence, immutable persistence, role boundaries and concurrent publication using the real server. Wrong-result mutations exercise the uncertainty and dataset-scope conditions. Pure tests do not establish deployment durability or workload capacity.
