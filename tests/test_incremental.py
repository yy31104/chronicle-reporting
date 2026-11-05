"""Calendar dependency expectations include absent and lookback months."""

import random
from datetime import date

import pytest

from chronicle.incremental import affected_months, selection_changes

OUTPUTS = tuple(date(2026, month, 1) for month in range(1, 5))


@pytest.mark.parametrize(
    "formula,changed,expected",
    [
        ("employment_delta", {date(2025, 12, 1)}, [1]),
        ("employment_delta", {date(2026, 1, 1)}, [1, 2]),
        ("production_mean3", {date(2025, 11, 1)}, [1]),
        ("production_mean3", {date(2025, 12, 1)}, [1, 2]),
        ("production_mean3", {date(2026, 1, 1)}, [1, 2, 3]),
        ("production_mean3", {date(2025, 10, 1)}, []),
    ],
)
def test_literal_footprints(formula, changed, expected):
    assert affected_months(formula, OUTPUTS, changed) == tuple(
        date(2026, month, 1) for month in expected
    )


def test_generated_footprints_equal_independent_month_index_scan():
    randomizer = random.Random(2147)
    for _ in range(100):
        changed = {
            date(2025 + (index // 12), index % 12 + 1, 1)
            for index in randomizer.sample(range(24), randomizer.randrange(8))
        }
        for formula, radius in (("employment_delta", 1), ("production_mean3", 2)):
            expected = tuple(
                month
                for month in OUTPUTS
                if any(
                    0 <= (month.year - item.year) * 12 + month.month - item.month <= radius
                    for item in changed
                )
            )
            assert affected_months(formula, OUTPUTS, changed) == expected


def test_absent_input_backfill_and_withdrawal_are_selection_changes():
    old = {
        "observation_month": "2026-01-01",
        "status": "not_available_at_cutoff",
        "value": None,
        "source_status": None,
        "unit_compatible": None,
        "unit": None,
        "availability_unknown": False,
        "selected_revision": None,
        "blocking_revision": None,
    }
    backfill = {
        **old,
        "status": "value",
        "value": "100",
        "source_status": "value",
        "unit_compatible": True,
        "unit": {"name": "index", "metadata_version": "v1"},
    }
    withdrawn = {**backfill, "status": "withdrawn", "value": None, "source_status": "withdrawn"}
    assert selection_changes([old], [backfill]) == {date(2026, 1, 1)}
    assert selection_changes([backfill], [withdrawn]) == {date(2026, 1, 1)}
