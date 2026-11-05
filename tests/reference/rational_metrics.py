"""Small independent arithmetic oracle using Fraction and builtin half-even round.

Lexical source decimals are kept by fixtures, never converted through binary floats.
This module is a reference, not a released production report implementation.
"""

import re
from collections.abc import Sequence
from fractions import Fraction

SCALE = 10**12
MAX_SCALED = 10**38 - 1
MAX_VALUE = Fraction(MAX_SCALED, SCALE)
_DECIMAL = re.compile(r"[+-]?[0-9]+(?:\.[0-9]{1,12})?\Z")


def source(value: str) -> Fraction:
    """Parse the explicit NUMERIC(38,12) source domain without silent rounding."""
    if not isinstance(value, str) or len(value) > 128 or not _DECIMAL.fullmatch(value):
        raise ValueError("invalid lexical decimal")
    parsed = Fraction(value)
    if abs(parsed) > MAX_VALUE:
        raise ValueError("NUMERIC_OUT_OF_RANGE")
    return parsed


def canonical(value: Fraction) -> str:
    """Round once to 12 places, using Python's exact Fraction rounding behavior."""
    if not isinstance(value, Fraction):
        raise TypeError("canonical requires exact Fraction")
    if abs(value) > MAX_VALUE:
        raise ValueError("NUMERIC_OUT_OF_RANGE")
    scaled = round(value * SCALE)
    if abs(scaled) > MAX_SCALED:
        raise ValueError("NUMERIC_OUT_OF_RANGE")
    sign = "-" if scaled < 0 else ""
    whole, fractional = divmod(abs(scaled), SCALE)
    return f"{sign}{whole}.{fractional:012d}"


def delta(current: str, previous: str) -> str:
    return canonical(source(current) - source(previous))


def mean(values: Sequence[str]) -> str:
    if not values:
        raise ValueError("mean needs all requested observations")
    exact = sum((source(value) for value in values), Fraction()) / len(values)
    return canonical(exact)


def yoy(current: str, previous_year: str) -> str:
    numerator, denominator = source(current), source(previous_year)
    if denominator == 0:
        raise ValueError("ZERO_DENOMINATOR")
    ratio = numerator / denominator
    if abs(ratio) > MAX_VALUE:
        raise ValueError("NUMERIC_OUT_OF_RANGE")
    return canonical(100 * (ratio - 1))
