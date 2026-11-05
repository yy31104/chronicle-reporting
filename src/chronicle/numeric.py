"""Exact NUMERIC(38, 12) primitives for the decimal12-half-even-v1 contract.

Source decimals are validated without rounding. Derived rational values are rounded
once, using integer remainder comparison. No binary floats or Decimal context take
part in either operation. These primitives do not implement report formulas.
"""

import re

from chronicle.errors import ContractError

SCALE = 10**12
MAX_SCALED = 10**38 - 1
MAX_SOURCE_LENGTH = 128
_SOURCE_DECIMAL = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")


def parse_scaled(value: str) -> int:
    """Parse a source lexical decimal into exact units of 10**-12.

    Accept a leading sign and leading zeros. Require at least one integer digit,
    and at least one fractional digit if a decimal point is present. Fractional
    digits beyond scale 12 are rejected even when they are zero. The lexical
    length bound also applies to leading zeros, before any integer conversion.
    """
    if not isinstance(value, str):
        raise ContractError("INVALID_DECIMAL", "Source decimal must be a string.")
    if len(value) > MAX_SOURCE_LENGTH:
        raise ContractError("NUMERIC_OUT_OF_RANGE", "Source decimal exceeds lexical length bound.")
    if _SOURCE_DECIMAL.fullmatch(value) is None:
        raise ContractError(
            "INVALID_DECIMAL", "Source decimal must use signed plain ASCII notation."
        )

    unsigned = value.removeprefix("-").removeprefix("+")
    integer, _, fractional = unsigned.partition(".")
    if len(integer.lstrip("0")) > 26 or len(fractional) > 12:
        raise ContractError("NUMERIC_OUT_OF_RANGE", "Source decimal exceeds NUMERIC(38, 12).")
    scaled = int(integer) * SCALE + int(fractional.ljust(12, "0"))
    if scaled > MAX_SCALED:
        raise ContractError("NUMERIC_OUT_OF_RANGE", "Source decimal exceeds NUMERIC(38, 12).")
    return -scaled if value.startswith("-") else scaled


def _require_integer(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError(
            "INVALID_DECIMAL", "Exact numeric arguments must be integers, not bools."
        )


def format_scaled(value: int) -> str:
    """Format an already scaled integer, with exactly 12 fractional digits."""
    _require_integer(value)
    if abs(value) > MAX_SCALED:
        raise ContractError("NUMERIC_OUT_OF_RANGE", "Canonical decimal exceeds NUMERIC(38, 12).")
    integer, fractional = divmod(abs(value), SCALE)
    return f"{'-' if value < 0 else ''}{integer}.{fractional:012d}"


def canonical_ratio(numerator: int, denominator: int = 1) -> str:
    """Round an exact real-unit ratio to a canonical 12-place decimal.

    The ratio itself must fit the signed NUMERIC(38, 12) magnitude boundary before
    rounding; integer numerator/denominator representations may exceed that bound.
    To convert scaled units, pass SCALE as the denominator. Negative zero renders
    as positive zero, and negative denominators are accepted.
    """
    _require_integer(numerator)
    _require_integer(denominator)
    if denominator == 0:
        raise ContractError("ZERO_DENOMINATOR", "An exact ratio requires a nonzero denominator.")

    magnitude = abs(numerator) * SCALE
    divisor = abs(denominator)
    if magnitude > MAX_SCALED * divisor:
        raise ContractError(
            "NUMERIC_OUT_OF_RANGE", "Exact ratio exceeds NUMERIC(38, 12) magnitude."
        )

    quotient, remainder = divmod(magnitude, divisor)
    twice_remainder = 2 * remainder
    if twice_remainder > divisor or (twice_remainder == divisor and quotient % 2):
        quotient += 1
    if (numerator < 0) != (denominator < 0):
        quotient = -quotient
    return format_scaled(quotient)
