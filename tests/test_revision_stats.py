"""Hand-authored expectations for first-release revision statistics (Aruoba 2008 style).

Expected decimals were derived by hand from the lexical inputs (square roots were
checked once with a 60-digit decimal calculation outside this suite) before the
production module was written. Inputs are literal; nothing here calls Chronicle
report, selection or normalization helpers.
"""

from copy import deepcopy
from fractions import Fraction

import pytest

from chronicle.errors import ContractError
from chronicle.revision_stats import (
    canonical_sqrt,
    derive_monthly_change,
    derive_trailing_mean,
    first_release_revisions,
    revision_statistics,
)


def bls_levels():
    """Total nonfarm SA levels from the two audited Employment Situation tables."""
    return [
        {
            "vintage_date": "2024-02-02",
            "reference_month": "2024-01-01",
            "values": {"2023-12-01": "157347", "2024-01-01": "157700"},
        },
        {
            "vintage_date": "2024-03-08",
            "reference_month": "2024-02-01",
            "values": {"2023-12-01": "157304", "2024-01-01": "157533", "2024-02-01": "157808"},
        },
    ]


def test_bls_monthly_change_derives_353_then_229_within_each_vintage():
    changes = derive_monthly_change(bls_levels())
    assert [v["values"] for v in changes] == [
        {"2024-01-01": Fraction(353)},
        {"2024-01-01": Fraction(229), "2024-02-01": Fraction(275)},
    ]
    assert [v["reference_month"] for v in changes] == ["2024-01-01", "2024-02-01"]


def test_january_payroll_change_revision_is_minus_124_down_with_undefined_sd():
    stats = revision_statistics(derive_monthly_change(bls_levels()))
    one, latest = stats["statistics"]
    assert one["horizon"] == "1" and latest["horizon"] == "latest"
    for row in (one, latest):
        assert row["n"] == 1
        assert row["mean"] == "-124.000000000000"
        assert row["standard_deviation"] is None  # sample sd needs n >= 2, never 0
        assert row["direction"] == {"up": 0, "down": 1, "unchanged": 0}
    (jan,) = stats["revisions"]
    assert jan["observation_month"] == "2024-01-01"
    assert jan["first_release_vintage"] == "2024-02-02"
    assert jan["first_release_value"] == "353.000000000000"
    assert jan["later"] == [
        {
            "vintage_date": "2024-03-08",
            "horizon": 1,
            "status": "value",
            "value": "229.000000000000",
            "revision": "-124.000000000000",
        }
    ]
    assert stats["no_later_vintage"] == ["2024-02-01"]


def test_level_series_separates_unretained_first_releases():
    stats = revision_statistics(bls_levels())
    assert [r["observation_month"] for r in stats["revisions"]] == ["2024-01-01"]
    assert stats["revisions"][0]["later"][0]["revision"] == "-167.000000000000"
    # December 2023 first appeared in the January 5 release, which is not retained.
    assert stats["first_release_not_retained"] == ["2023-12-01"]
    assert stats["statistics"][0]["mean"] == "-167.000000000000"


def vintages(revisions):
    """One first release per month (value 0) and one later vintage with `revisions`."""
    months = [f"2020-{i:02d}-01" for i in range(1, len(revisions) + 1)]
    result = [
        {"vintage_date": f"2020-{i + 1:02d}-15", "reference_month": m, "values": {m: "0"}}
        for i, m in enumerate(months)
    ]
    result.append(
        {
            "vintage_date": "2021-06-15",
            "reference_month": "2021-05-01",
            "values": dict(zip(months, revisions, strict=True)),
        }
    )
    return result


def test_mean_and_sample_sd_are_exact_then_rounded_once():
    # revisions 1, 2, 4: mean 7/3; squared deviations 16/9 + 1/9 + 25/9 = 14/3;
    # sample variance 7/3; sd sqrt(7/3) = 1.5275252316519466... -> 1.527525231652.
    (row, _) = revision_statistics(vintages(["1", "2", "4"]))["statistics"]
    assert row["horizon"] == "1"
    assert row["n"] == 3
    assert row["mean"] == "2.333333333333"
    assert row["standard_deviation"] == "1.527525231652"
    assert row["direction"] == {"up": 3, "down": 0, "unchanged": 0}


def test_exact_square_root_and_mixed_direction():
    # revisions -2, 0, 2 -> mean 0, sample variance (4 + 0 + 4) / 2 = 4, sd exactly 2.
    (row, _) = revision_statistics(vintages(["-2", "0", "2"]))["statistics"]
    assert row["mean"] == "0.000000000000"
    assert row["standard_deviation"] == "2.000000000000"
    assert row["direction"] == {"up": 1, "down": 1, "unchanged": 1}


def test_two_revisions_irrational_sd():
    # revisions 1, 3 -> mean 2, var 2, sd sqrt(2) = 1.41421356237309504... -> 1.414213562373
    (row, _) = revision_statistics(vintages(["1", "3"]))["statistics"]
    assert row["standard_deviation"] == "1.414213562373"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Fraction(625, 10**26), "0.000000000002"),  # sqrt = 2.5e-12, tie -> even 2
        (Fraction(1225, 10**26), "0.000000000004"),  # sqrt = 3.5e-12, tie -> even 4
        (Fraction(4), "2.000000000000"),
        (Fraction(0), "0.000000000000"),
    ],
)
def test_canonical_sqrt_rounds_half_even_once(value, expected):
    assert canonical_sqrt(value) == expected


def test_canonical_sqrt_rejects_negative():
    with pytest.raises(ContractError) as failure:
        canonical_sqrt(Fraction(-1))
    assert failure.value.code == "INVALID_DECIMAL"


@pytest.mark.parametrize("status", ["missing", "withdrawn"])
def test_unavailable_later_value_is_counted_not_zero_filled(status):
    data = vintages(["1", "3", "5"])
    data[-1]["values"]["2020-03-01"] = {"status": status}
    stats = revision_statistics(data)
    row = stats["statistics"][0]
    assert row["n"] == 2
    assert row["unavailable"] == 1
    assert row["mean"] == "2.000000000000"  # (1 + 3) / 2, not (1 + 3 + 0) / 3
    assert stats["revisions"][2]["later"][0] == {
        "vintage_date": "2021-06-15",
        "horizon": 1,
        "value": None,
        "status": status,
        "revision": None,
    }


def test_unavailable_first_release_is_not_a_first_release_value():
    data = vintages(["1", "3"])
    data[0]["values"]["2020-01-01"] = {"status": "missing"}
    stats = revision_statistics(data)
    assert stats["first_release_unavailable"] == ["2020-01-01"]
    assert stats["statistics"][0]["n"] == 1


def test_empty_horizon_has_null_mean():
    stats = revision_statistics([bls_levels()[0]])
    assert stats["statistics"] == [
        {
            "horizon": "latest",
            "n": 0,
            "unavailable": 0,
            "mean": None,
            "standard_deviation": None,
            "direction": {"up": 0, "down": 0, "unchanged": 0},
        }
    ]


def test_vintages_must_be_strictly_ordered_and_reference_months_unique():
    data = bls_levels()
    data.reverse()
    with pytest.raises(ContractError) as failure:
        first_release_revisions(data)
    assert failure.value.code == "INVALID_VINTAGES"
    data = bls_levels()
    data[1]["reference_month"] = "2024-01-01"
    with pytest.raises(ContractError) as failure:
        first_release_revisions(data)
    assert failure.value.code == "INVALID_VINTAGES"


def test_inputs_are_not_mutated():
    data = bls_levels()
    before = deepcopy(data)
    revision_statistics(derive_monthly_change(data))
    assert data == before


def frb_index():
    """G.17 total industrial production index, vintages 2024-02-15 and 2024-03-15."""
    return [
        {
            "vintage_date": "2024-02-15",
            "reference_month": "2024-01-01",
            "values": {"2023-11-01": "102.7", "2023-12-01": "102.7", "2024-01-01": "102.6"},
        },
        {
            "vintage_date": "2024-03-15",
            "reference_month": "2024-02-01",
            "values": {
                "2023-11-01": "103.0",
                "2023-12-01": "102.7",
                "2024-01-01": "102.2",
                "2024-02-01": "102.3",
            },
        },
    ]


def test_trailing_mean_revision_is_exact_minus_one_thirtieth_rounded_once():
    # 308.0/3 -> 307.9/3: exact revision -1/30 = -0.0333... -> -0.033333333333.
    # Subtracting the rounded means (102.633333333333 - 102.666666666667) would give
    # -0.033333333334; this statistic must not.
    means = derive_trailing_mean(frb_index())
    assert means[0]["values"] == {"2024-01-01": Fraction(308, 3)}
    assert means[1]["values"] == {"2024-01-01": Fraction(3079, 30), "2024-02-01": Fraction(512, 5)}
    stats = revision_statistics(means)
    (jan,) = stats["revisions"]
    assert jan["first_release_value"] == "102.666666666667"
    assert jan["later"][0]["value"] == "102.633333333333"
    assert jan["later"][0]["revision"] == "-0.033333333333"
    assert stats["statistics"][0]["mean"] == "-0.033333333333"


def test_frb_index_first_release_revision_is_minus_point_four():
    stats = revision_statistics(frb_index())
    assert stats["revisions"][0]["later"][0]["revision"] == "-0.400000000000"
    assert stats["first_release_not_retained"] == ["2023-11-01", "2023-12-01"]
    assert stats["no_later_vintage"] == ["2024-02-01"]
