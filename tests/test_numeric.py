"""Numeric boundary corpus, with an independent Fraction/round reference."""

from decimal import localcontext
from fractions import Fraction
from random import Random

import pytest

from chronicle.errors import ContractError
from chronicle.numeric import canonical_ratio, format_scaled, parse_scaled


def reference_canonical(numerator: int, denominator: int) -> str:
    """Fraction.__round__ supplies an independent exact half-even implementation."""
    rounded = round(Fraction(numerator, denominator) * 1_000_000_000_000)
    digits = str(abs(rounded)).zfill(13)
    return ("-" if rounded < 0 else "") + digits[:-12] + "." + digits[-12:]


@pytest.mark.parametrize(
    ("lexical", "scaled", "canonical"),
    [
        ("0", 0, "0.000000000000"),
        ("-0.000000000000", 0, "0.000000000000"),
        ("+001.2300", 1_230_000_000_000, "1.230000000000"),
        ("-12.5", -12_500_000_000_000, "-12.500000000000"),
        ("0.000000000001", 1, "0.000000000001"),
        ("-0.000000000001", -1, "-0.000000000001"),
        (
            "99999999999999999999999999.999999999999",
            99_999_999_999_999_999_999_999_999_999_999_999_999,
            "99999999999999999999999999.999999999999",
        ),
        (
            "-99999999999999999999999999.999999999999",
            -99_999_999_999_999_999_999_999_999_999_999_999_999,
            "-99999999999999999999999999.999999999999",
        ),
    ],
)
def test_lexical_values_are_exact(lexical, scaled, canonical):
    assert parse_scaled(lexical) == scaled
    assert format_scaled(scaled) == canonical


@pytest.mark.parametrize(
    "lexical",
    [
        "",
        " ",
        " 1",
        "1 ",
        "1\n",
        ".1",
        "1.",
        "1e2",
        "NaN",
        "Infinity",
        "1,000",
        "1_000",
        "--1",
        "+-1",
        "１",
        "١",
        "1.2.3",
        None,
        True,
        1,
        1.5,
    ],
)
def test_non_contract_decimal_is_rejected(lexical):
    with pytest.raises(ContractError) as caught:
        parse_scaled(lexical)
    assert caught.value.code == "INVALID_DECIMAL"


@pytest.mark.parametrize(
    "lexical",
    [
        "100000000000000000000000000",
        "-100000000000000000000000000",
        "0.0000000000001",
        "1.0000000000000",
        "0" * 129,
        "9" * 10_000,
        "0" * 10_000,
    ],
)
def test_source_out_of_range_or_scale_is_never_rounded(lexical):
    with pytest.raises(ContractError) as caught:
        parse_scaled(lexical)
    assert caught.value.code == "NUMERIC_OUT_OF_RANGE"


def test_source_lexical_length_limit_is_explicit():
    assert parse_scaled("0" * 128) == 0


def test_source_precision_is_independent_of_decimal_context():
    with localcontext() as context:
        context.prec = 2
        assert parse_scaled("20000000000000100000000000.000000000003") == (
            20_000_000_000_000_100_000_000_000_000_000_000_003
        )


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [
        (0, 1, "0.000000000000"),
        (1, 3, "0.333333333333"),
        (2, 3, "0.666666666667"),
        (-1, 3, "-0.333333333333"),
        (1, -3, "-0.333333333333"),
        (-2, -3, "0.666666666667"),
        # Exact half-even ties around even and odd last retained digits.
        (1, 2_000_000_000_000, "0.000000000000"),
        (-1, 2_000_000_000_000, "0.000000000000"),
        (3, 2_000_000_000_000, "0.000000000002"),
        (-3, 2_000_000_000_000, "-0.000000000002"),
        (5, 2_000_000_000_000, "0.000000000002"),
        (-5, 2_000_000_000_000, "-0.000000000002"),
        # Values immediately either side of ties, with both signs.
        (4999, 10_000_000_000_000_000, "0.000000000000"),
        (5001, 10_000_000_000_000_000, "0.000000000001"),
        (-4999, 10_000_000_000_000_000, "0.000000000000"),
        (-5001, 10_000_000_000_000_000, "-0.000000000001"),
        (14999, 10_000_000_000_000_000, "0.000000000001"),
        (15001, 10_000_000_000_000_000, "0.000000000002"),
        (-14999, 10_000_000_000_000_000, "-0.000000000001"),
        (-15001, 10_000_000_000_000_000, "-0.000000000002"),
        (1_999_999_999_999, 2_000_000_000_000, "1.000000000000"),
    ],
)
def test_once_only_half_even_rounding(numerator, denominator, expected):
    assert reference_canonical(numerator, denominator) == expected
    assert canonical_ratio(numerator, denominator) == expected


def test_f2_counterexample_uses_exact_ratio_without_shipping_a_yoy_formula():
    # Hand-authored review F2 input and expected output. Fraction parses lexical
    # inputs separately; it does not call the production decimal parser.
    x = Fraction("20000000000000100000000000.000000000003")
    y = Fraction("200.000000000001")
    ratio = 100 * (x / y - 1)
    expected = "9999999999999999999999900.000000000001"
    assert reference_canonical(ratio.numerator, ratio.denominator) == expected
    assert canonical_ratio(ratio.numerator, ratio.denominator) == expected


def test_exact_ratio_matches_independent_reference_on_deterministic_corpus():
    generator = Random(20261023)
    for _ in range(500):
        numerator = generator.randrange(-(10**38), 10**38)
        denominator = generator.randrange(10**13, 10**30)
        if generator.randrange(2):
            denominator = -denominator
        assert canonical_ratio(numerator, denominator) == reference_canonical(
            numerator, denominator
        )


def test_zero_denominator_has_specific_error():
    with pytest.raises(ContractError) as caught:
        canonical_ratio(1, 0)
    assert caught.value.code == "ZERO_DENOMINATOR"


@pytest.mark.parametrize("sign", [-1, 1])
def test_exact_and_canonical_overflow_rejected(sign):
    with pytest.raises(ContractError) as caught:
        canonical_ratio(sign * 10**26)
    assert caught.value.code == "NUMERIC_OUT_OF_RANGE"
    # Even a ratio that would round back to the limit is outside the exact domain.
    with pytest.raises(ContractError) as caught:
        canonical_ratio(sign * (10 * (10**38 - 1) + 1), 10**13)
    assert caught.value.code == "NUMERIC_OUT_OF_RANGE"


@pytest.mark.parametrize("sign", [-1, 1])
def test_exact_domain_limits_are_accepted(sign):
    expected = ("-" if sign < 0 else "") + "99999999999999999999999999.999999999999"
    assert canonical_ratio(sign * (10**38 - 1), 10**12) == expected


def test_integer_ratio_representation_need_not_fit_output_domain():
    assert canonical_ratio(10**200, 10**200) == "1.000000000000"


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None])
def test_noninteger_ratio_and_scaled_arguments_rejected(value):
    for operation in (
        lambda: canonical_ratio(value),
        lambda: canonical_ratio(1, value),
        lambda: format_scaled(value),
    ):
        with pytest.raises(ContractError) as caught:
            operation()
        assert caught.value.code == "INVALID_DECIMAL"


@pytest.mark.parametrize("value", [10**38, -(10**38)])
def test_scaled_output_overflow_rejected(value):
    with pytest.raises(ContractError) as caught:
        format_scaled(value)
    assert caught.value.code == "NUMERIC_OUT_OF_RANGE"
