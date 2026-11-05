"""Immutable C1 input records, independent of database and query implementations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from chronicle.errors import ContractError
from chronicle.numeric import parse_scaled


def _identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > 200:
        raise ContractError("INVALID_IDENTITY", f"{field} must be 1–200 non-padded characters")


def _date(value: date, field: str) -> None:
    if type(value) is not date:
        raise ContractError("INVALID_DATE", f"{field} requires a date, without an invented clock")


def _month(value: date) -> None:
    _date(value, "observation_month")
    if value.day != 1:
        raise ContractError("INVALID_MONTH", "observation_month must be the first of a month")


class AvailabilityPrecision(StrEnum):
    DATE = "date"
    UNKNOWN = "unknown"


class RevisionStatus(StrEnum):
    VALUE = "value"
    MISSING = "missing"
    WITHDRAWN = "withdrawn"


class KnowledgeMode(StrEnum):
    LATEST_CORRECTED = "latest_corrected"
    SOURCE_AS_OF = "source_as_of"
    SYSTEM_SNAPSHOT = "system_snapshot"


class CellStatus(StrEnum):
    VALUE = "value"
    MISSING = "missing"
    WITHDRAWN = "withdrawn"
    NOT_AVAILABLE_AT_CUTOFF = "not_available_at_cutoff"
    UNKNOWN_AVAILABILITY = "unknown_availability"
    UNIT_MISMATCH = "unit_mismatch"
    INCOMPATIBLE_SCHEMA = "incompatible_schema"


class OrderRule(StrEnum):
    DATE_ORDINAL = "date_ordinal"
    DOCUMENTED_SEQUENCE = "documented_sequence"


@dataclass(frozen=True)
class SourceAvailability:
    precision: AvailabilityPrecision
    date: date | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "precision", AvailabilityPrecision(self.precision))
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "UNSUPPORTED_PRECISION", "C1 accepts date or unknown evidence"
            ) from exc
        if self.precision is AvailabilityPrecision.DATE:
            _date(self.date, "source availability")
        elif self.date is not None:
            raise ContractError(
                "INVALID_AVAILABILITY", "unknown availability cannot contain a date"
            )


@dataclass(frozen=True)
class SourceCutoff:
    exclusive_end_date: date

    def __post_init__(self) -> None:
        if isinstance(self.exclusive_end_date, datetime):
            raise ContractError("UNSUPPORTED_PRECISION", "C1 does not accept intraday cutoffs")
        _date(self.exclusive_end_date, "exclusive_end_date")


@dataclass(frozen=True)
class Unit:
    name: str
    metadata_version: str

    def __post_init__(self) -> None:
        _identifier(self.name, "unit name")
        _identifier(self.metadata_version, "unit metadata version")


@dataclass(frozen=True)
class Occurrence:
    batch_id: str
    received_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.batch_id, "batch_id")
        if not isinstance(self.received_at, datetime) or self.received_at.utcoffset() is None:
            raise ContractError("INVALID_INSTANT", "received_at must be timezone-aware")
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))


@dataclass(frozen=True)
class Revision:
    source_namespace: str
    series_key: str
    observation_month: date
    source_revision_key: str
    source_order: int
    order_rule: OrderRule
    availability: SourceAvailability
    value: str | None
    status: RevisionStatus
    unit: Unit
    occurrences: tuple[Occurrence, ...]

    def __post_init__(self) -> None:
        for field in ("source_namespace", "series_key", "source_revision_key"):
            _identifier(getattr(self, field), field)
        _month(self.observation_month)
        if type(self.source_order) is not int or not -(2**63) <= self.source_order < 2**63:
            raise ContractError("INVALID_SOURCE_ORDER", "source_order requires an evidenced BIGINT")
        try:
            object.__setattr__(self, "status", RevisionStatus(self.status))
            object.__setattr__(self, "order_rule", OrderRule(self.order_rule))
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "INVALID_REVISION", "status and order rule must be explicit"
            ) from exc
        if not isinstance(self.availability, SourceAvailability) or not isinstance(self.unit, Unit):
            raise ContractError("INVALID_REVISION", "typed availability and unit are required")
        if self.order_rule is OrderRule.DATE_ORDINAL:
            if self.availability.precision is not AvailabilityPrecision.DATE:
                raise ContractError("INVALID_SOURCE_ORDER", "date ordering requires a known date")
            if self.source_order != self.availability.date.toordinal():
                raise ContractError(
                    "INVALID_SOURCE_ORDER", "date order must equal the date ordinal"
                )
        if self.status is RevisionStatus.VALUE:
            parse_scaled(self.value)
        elif self.value is not None:
            raise ContractError(
                "INVALID_VALUE_STATUS", "missing/withdrawn revisions have null value"
            )
        if not isinstance(self.occurrences, tuple) or not self.occurrences:
            raise ContractError("INVALID_OCCURRENCE", "a revision requires immutable occurrences")
        if any(not isinstance(item, Occurrence) for item in self.occurrences):
            raise ContractError(
                "INVALID_OCCURRENCE", "occurrences must contain typed receipt records"
            )
        if len(set(self.occurrences)) != len(self.occurrences):
            raise ContractError("INVALID_OCCURRENCE", "duplicate receipt occurrence")

    @property
    def natural_key(self) -> tuple[str, str, date, str]:
        return (
            self.source_namespace,
            self.series_key,
            self.observation_month,
            self.source_revision_key,
        )

    @property
    def content(self) -> tuple:
        """Compare source content exactly; receipts are provenance, not new values."""
        return (
            self.source_order,
            self.order_rule,
            self.availability,
            self.value,
            self.status,
            self.unit,
        )


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    batch_ids: frozenset[str]

    def __post_init__(self) -> None:
        _identifier(self.snapshot_id, "snapshot_id")
        if not isinstance(self.batch_ids, frozenset):
            raise ContractError("INVALID_SNAPSHOT", "snapshot membership must be immutable")
        for batch_id in self.batch_ids:
            _identifier(batch_id, "batch_id")


@dataclass(frozen=True)
class Query:
    mode: KnowledgeMode
    snapshot_id: str
    source_namespace: str
    series_key: str
    months: tuple[date, ...]
    cutoff: SourceCutoff | None = None
    expected_unit: Unit | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "mode", KnowledgeMode(self.mode))
        except (TypeError, ValueError) as exc:
            raise ContractError("INVALID_MODE", "select an explicit knowledge mode") from exc
        for field in ("snapshot_id", "source_namespace", "series_key"):
            _identifier(getattr(self, field), field)
        if not isinstance(self.months, tuple) or not self.months:
            raise ContractError("INVALID_GRID", "request an explicit nonempty immutable month grid")
        for month in self.months:
            _month(month)
        if len(set(self.months)) != len(self.months):
            raise ContractError("INVALID_GRID", "month grid cannot contain duplicates")
        if self.mode is KnowledgeMode.SOURCE_AS_OF:
            if not isinstance(self.cutoff, SourceCutoff):
                raise ContractError("CUTOFF_REQUIRED", "source_as_of requires a date cutoff")
        elif self.cutoff is not None:
            raise ContractError("UNEXPECTED_CUTOFF", "only source_as_of accepts a source cutoff")
        if self.expected_unit is not None and not isinstance(self.expected_unit, Unit):
            raise ContractError(
                "INVALID_UNIT", "expected_unit must include name and metadata version"
            )
