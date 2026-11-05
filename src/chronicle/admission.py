"""Pure immutable validation of revision identity, without persistence claims."""

from dataclasses import dataclass, replace

from chronicle.domain import Revision
from chronicle.errors import ContractError


@dataclass(frozen=True)
class RevisionCatalog:
    revisions: tuple[Revision, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.revisions, tuple) or any(
            not isinstance(row, Revision) for row in self.revisions
        ):
            raise ContractError("INVALID_CATALOG", "catalog requires immutable typed revisions")
        for index, row in enumerate(self.revisions):
            for existing in self.revisions[:index]:
                self._check_pair(existing, row)
                if existing.natural_key == row.natural_key:
                    raise ContractError("DUPLICATE_REVISION", "use admit to merge reobservations")

    @staticmethod
    def _check_pair(existing: Revision, candidate: Revision) -> None:
        if existing.natural_key == candidate.natural_key and existing.content != candidate.content:
            raise ContractError(
                "REVISION_CONFLICT", "equal revision identity has different content"
            )
        if existing.source_namespace != candidate.source_namespace:
            return
        if existing.order_rule != candidate.order_rule:
            raise ContractError(
                "AMBIGUOUS_SOURCE_ORDER", "a source namespace requires one comparable order rule"
            )
        if existing.source_revision_key == candidate.source_revision_key:
            if (existing.source_order, existing.order_rule, existing.availability) != (
                candidate.source_order,
                candidate.order_rule,
                candidate.availability,
            ):
                raise ContractError("RELEASE_CONFLICT", "one release must have consistent evidence")
        elif existing.source_order == candidate.source_order:
            raise ContractError("AMBIGUOUS_SOURCE_ORDER", "distinct releases cannot share an order")

    def admit(self, candidate: Revision) -> "RevisionCatalog":
        """Return a new catalog; rejection leaves the caller's old value unchanged."""
        if not isinstance(candidate, Revision):
            raise ContractError("INVALID_REVISION", "candidate must be a typed revision")
        for index, existing in enumerate(self.revisions):
            self._check_pair(existing, candidate)
            if existing.natural_key == candidate.natural_key:
                receipts = tuple(dict.fromkeys(existing.occurrences + candidate.occurrences))
                merged = replace(existing, occurrences=receipts)
                return RevisionCatalog(
                    self.revisions[:index] + (merged,) + self.revisions[index + 1 :]
                )
        return RevisionCatalog(self.revisions + (candidate,))
