"""First-release revision statistics in the style of Aruoba (2008), with exact arithmetic.

Aruoba (2008) defines a revision as a later estimate minus the initial announcement
and asks whether revisions have zero mean, small variance and no predictability.
This module computes the descriptive part for retained vintages: for each
observation month whose first release is retained, the revision to every later
retained vintage that reports it, then per horizon the count, mean, sample standard
deviation and direction counts.

Rules that make the numbers auditable:

- A vintage declares its ``reference_month``: the newest month it first announces.
  An observation's first release is the vintage whose reference month equals it.
  Months that appear only as already-revised values are listed in
  ``first_release_not_retained`` and excluded from the statistics.
- Horizon ``h`` is the h-th later *retained* vintage that reports the month, not the
  h-th publisher release; ``latest`` is the last retained vintage (Aruoba's "final"
  proxy within this archive).
- Values are lexical decimals parsed exactly. Means and variances are exact
  rationals; each published statistic is rounded once (decimal12 half-even). The
  sample standard deviation is the once-rounded square root of the exact sample
  variance and is null, never zero, when fewer than two revisions exist.
- ``missing`` and ``withdrawn`` values are counted as unavailable and never become
  zero.
"""

from datetime import date
from fractions import Fraction
from math import isqrt

from chronicle.errors import ContractError
from chronicle.numeric import SCALE, canonical_ratio, format_scaled, parse_scaled

POLICY = "exact-rational-statistics-round-once-decimal12-half-even-v1"


def _canonical(value: Fraction) -> str:
    return canonical_ratio(value.numerator, value.denominator)


def canonical_sqrt(value: Fraction) -> str:
    """Round the square root of an exact non-negative rational once, half-even."""
    if value < 0:
        raise ContractError("INVALID_DECIMAL", "Square root requires a non-negative value.")
    scaled = value * SCALE * SCALE  # sqrt(scaled) = sqrt(value) * SCALE
    p, q = scaled.numerator, scaled.denominator
    root = isqrt(p // q)  # floor(sqrt(p / q))
    # Compare sqrt(p/q) with root + 1/2, i.e. 4p with (2 root + 1)^2 q, exactly.
    left, right = 4 * p, (2 * root + 1) ** 2 * q
    if left > right or (left == right and root % 2):
        root += 1
    return format_scaled(root)


def _value(raw) -> tuple[str, Fraction | None]:
    if isinstance(raw, Fraction):  # exact derived value; never pre-rounded
        return "value", raw
    if isinstance(raw, str):
        return "value", Fraction(parse_scaled(raw), SCALE)
    status = raw.get("status") if isinstance(raw, dict) else None
    if status not in ("missing", "withdrawn", "incomplete"):
        raise ContractError("INVALID_EVIDENCE", "Unsupported vintage value.")
    return status, None


def _validate(vintages: list[dict]) -> None:
    dates = [date.fromisoformat(v["vintage_date"]) for v in vintages]
    if any(a >= b for a, b in zip(dates, dates[1:], strict=False)):
        raise ContractError("INVALID_VINTAGES", "Vintages must be strictly ordered by date.")
    references = [v["reference_month"] for v in vintages]
    if len(set(references)) != len(references):
        raise ContractError("INVALID_VINTAGES", "Each vintage needs its own reference month.")


def _previous_month(month: str) -> str:
    value = date.fromisoformat(month)
    index = value.year * 12 + value.month - 2
    return date(index // 12, index % 12 + 1, 1).isoformat()


def derive_monthly_change(vintages: list[dict]) -> list[dict]:
    """Within each vintage, change(m) = level(m) - level(m - 1), as exact rationals."""
    _validate(vintages)
    result = []
    for vintage in vintages:
        values = vintage["values"]
        changes = {}
        for month in sorted(values):
            previous = _previous_month(month)
            if previous not in values:
                continue
            (status_now, now), (status_before, before) = (
                _value(values[month]),
                _value(values[previous]),
            )
            if status_now == status_before == "value":
                changes[month] = now - before
            else:
                changes[month] = {"status": "incomplete"}
        result.append({**vintage, "values": changes})
    return result


def derive_trailing_mean(vintages: list[dict], window: int = 3) -> list[dict]:
    """Within each vintage, the exact mean of months m - window + 1 .. m."""
    _validate(vintages)
    result = []
    for vintage in vintages:
        values = vintage["values"]
        means = {}
        for month in sorted(values):
            span = [month]
            for _ in range(window - 1):
                span.append(_previous_month(span[-1]))
            if any(m not in values for m in span):
                continue
            parsed = [_value(values[m]) for m in span]
            if all(status == "value" for status, _ in parsed):
                means[month] = sum((x for _, x in parsed), Fraction()) / window
            else:
                means[month] = {"status": "incomplete"}
        result.append({**vintage, "values": means})
    return result


def first_release_revisions(vintages: list[dict]) -> dict:
    """List each retained first release and its revisions in later retained vintages."""
    _validate(vintages)
    position = {v["reference_month"]: i for i, v in enumerate(vintages)}
    months = sorted({month for v in vintages for month in v["values"]})
    revisions, not_retained, unavailable, no_later = [], [], [], []
    for month in months:
        first = position.get(month)
        reported = [i for i, v in enumerate(vintages) if month in v["values"]]
        if first is None or first not in reported:
            not_retained.append(month)
            continue
        if reported[0] != first:
            raise ContractError(
                "INVALID_VINTAGES", "A month appears before the vintage that first announces it."
            )
        status, initial = _value(vintages[first]["values"][month])
        if status != "value":
            unavailable.append(month)
            continue
        later = []
        for horizon, index in enumerate(reported[1:], start=1):
            later_status, value = _value(vintages[index]["values"][month])
            later.append(
                {
                    "vintage_date": vintages[index]["vintage_date"],
                    "horizon": horizon,
                    "status": later_status,
                    "value": None if value is None else _canonical(value),
                    "revision": None if value is None else _canonical(value - initial),
                    "_exact": None if value is None else value - initial,
                }
            )
        if not later:
            no_later.append(month)
            continue
        revisions.append(
            {
                "observation_month": month,
                "first_release_vintage": vintages[first]["vintage_date"],
                "first_release_value": _canonical(initial),
                "later": later,
            }
        )
    return {
        "revisions": revisions,
        "first_release_not_retained": not_retained,
        "first_release_unavailable": unavailable,
        "no_later_vintage": no_later,
    }


def _summary(horizon: str, entries: list[dict]) -> dict:
    exact = [e["_exact"] for e in entries if e["_exact"] is not None]
    n = len(exact)
    mean = sum(exact, Fraction()) / n if n else None
    sd = None
    if n >= 2:
        sd = canonical_sqrt(sum(((x - mean) ** 2 for x in exact), Fraction()) / (n - 1))
    return {
        "horizon": horizon,
        "n": n,
        "unavailable": len(entries) - n,
        "mean": None if mean is None else _canonical(mean),
        "standard_deviation": sd,
        "direction": {
            "up": sum(1 for x in exact if x > 0),
            "down": sum(1 for x in exact if x < 0),
            "unchanged": sum(1 for x in exact if x == 0),
        },
    }


def revision_statistics(vintages: list[dict]) -> dict:
    """Per-horizon mean, sample standard deviation and direction of revisions."""
    found = first_release_revisions(vintages)
    by_horizon: dict[int, list[dict]] = {}
    latest = []
    for record in found["revisions"]:
        for entry in record["later"]:
            by_horizon.setdefault(entry["horizon"], []).append(entry)
        latest.append(record["later"][-1])
    statistics = [_summary(str(h), by_horizon[h]) for h in sorted(by_horizon)]
    statistics.append(_summary("latest", latest))
    for record in found["revisions"]:
        record["later"] = [{k: v for k, v in e.items() if k != "_exact"} for e in record["later"]]
    return {"policy": POLICY, **found, "statistics": statistics}
