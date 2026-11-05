"""Stable failures and a closed, structured PostgreSQL translation boundary."""

import re
from contextlib import contextmanager

import psycopg


class ContractError(ValueError):
    """Rejected input with a machine-readable reason; no state was published."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RetryableTransactionError(RuntimeError):
    """The entire transaction failed; retry with its original identity, if desired."""

    code = "TRANSACTION_RETRY_REQUIRED"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("The transaction must be retried in full.")


# Keys include SQLSTATE, schema, table and constraint. A similarly named constraint
# in another schema/table is not authority to classify an error as user input.
_CONSTRAINT_CODES = {
    (
        "P0001",
        "storage_object",
        "object_artifact_publication_required",
    ): "ARTIFACT_PUBLICATION_REQUIRED",
    ("23505", "source_release", "source_release_order_unique"): "AMBIGUOUS_SOURCE_ORDER",
    ("23503", "import_batch", "import_batch_dataset_id_fkey"): "UNKNOWN_DATASET",
    ("23503", "source_namespace", "source_namespace_dataset_id_fkey"): "UNKNOWN_DATASET",
    ("23514", "dataset", "dataset_dataset_id_check"): "INVALID_IDENTITY",
    ("23514", "source_namespace", "source_namespace_source_namespace_check"): "INVALID_IDENTITY",
    ("23514", "source_namespace", "source_namespace_order_rule_check"): "INVALID_ORDER_RULE",
    ("23514", "series", "series_series_key_check"): "INVALID_IDENTITY",
    ("23514", "source_release", "source_release_source_revision_key_check"): "INVALID_IDENTITY",
    (
        "23514",
        "source_release",
        "source_release_availability_precision_check",
    ): "UNSUPPORTED_PRECISION",
    ("23514", "source_release", "source_release_availability_consistent"): "INVALID_AVAILABILITY",
    ("23514", "source_release", "source_release_order_matches_date"): "INVALID_SOURCE_ORDER",
    ("23514", "revision", "revision_observation_month_check"): "INVALID_MONTH",
    ("23514", "revision", "revision_status_check"): "INVALID_STATUS",
    ("23514", "revision", "revision_unit_name_check"): "INVALID_UNIT",
    ("23514", "revision", "revision_metadata_version_check"): "INVALID_UNIT",
    ("23514", "revision", "revision_value_matches_status"): "INVALID_REVISION_VALUE",
    ("23514", "import_batch", "import_batch_batch_id_check"): "INVALID_IDENTITY",
    ("23514", "import_batch", "import_batch_content_sha256_check"): "INVALID_MANIFEST_HASH",
    (
        "23514",
        "import_batch",
        "import_batch_expected_occurrence_count_check",
    ): "INVALID_OCCURRENCE_COUNT",
    ("23514", "revision_occurrence", "revision_occurrence_received_at_check"): "INVALID_RECEIPT",
    ("P0001", "import_batch", "api_seal_requires_read_committed"): "SEAL_REQUIRES_READ_COMMITTED",
    ("23503", "import_batch", "api_batch_not_found"): "BATCH_NOT_FOUND",
    ("23514", "import_batch", "api_batch_incomplete"): "BATCH_INCOMPLETE",
}
for _table, _codes in (
    (
        "snapshot",
        (
            "INVALID_IDENTITY",
            "INVALID_MANIFEST_HASH",
            "INVALID_SNAPSHOT_MEMBERSHIP",
            "UNKNOWN_DATASET",
            "SNAPSHOT_IDENTITY_CONFLICT",
            "SNAPSHOT_BASE_CHANGED",
            "UNVALIDATED_SNAPSHOT_BATCH",
        ),
    ),
    (
        "analysis_context",
        (
            "INVALID_IDENTITY",
            "INVALID_MODE",
            "SNAPSHOT_REQUIRED",
            "CUTOFF_REQUIRED",
            "UNEXPECTED_CUTOFF",
            "INVALID_DATE",
            "INVALID_GRID",
            "INVALID_UNIT",
            "UNKNOWN_DATASET",
            "NO_CURRENT_SNAPSHOT",
            "UNKNOWN_SNAPSHOT",
            "CONTEXT_IDENTITY_CONFLICT",
        ),
    ),
):
    for _code in _codes:
        _CONSTRAINT_CODES[("P0001", _table, "api_" + _code.lower())] = _code


@contextmanager
def database_errors():
    """Wrap *outside* a transaction so rollback finishes before translation.

    Unknown database exceptions propagate unchanged for internal diagnostics.
    Delivery adapters must use public_error instead of rendering their text.
    No transaction retry is performed here, particularly not a partial retry.
    """
    try:
        yield
    except psycopg.Error as exc:
        if exc.sqlstate in {"40001", "40P01"}:
            reason = "SERIALIZATION_FAILURE" if exc.sqlstate == "40001" else "DEADLOCK_DETECTED"
            raise RetryableTransactionError(reason) from None
        code = None
        if exc.diag.schema_name == "chronicle":
            code = _CONSTRAINT_CODES.get(
                (exc.sqlstate, exc.diag.table_name, exc.diag.constraint_name)
            )
        if code:
            raise ContractError(code, code) from None
        raise


def public_error(exc: Exception) -> dict:
    """Return a safe delivery envelope without driver messages, SQL or values."""
    if isinstance(exc, psycopg.Error) and exc.sqlstate in {"40001", "40P01"}:
        reason = "SERIALIZATION_FAILURE" if exc.sqlstate == "40001" else "DEADLOCK_DETECTED"
        return public_error(RetryableTransactionError(reason))
    if isinstance(exc, ContractError):
        code = exc.code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", exc.code) else "INVALID_REQUEST"
        return {
            "code": code,
            "message": "The request violates Chronicle's contract.",
            "retryable": False,
        }
    if isinstance(exc, RetryableTransactionError):
        return {
            "code": exc.code,
            "message": "Retry the complete request with the same identity.",
            "retryable": True,
        }
    if isinstance(exc, psycopg.Error):
        return {
            "code": "DATABASE_ERROR",
            "message": "The database operation failed.",
            "retryable": False,
        }
    return {
        "code": "INTERNAL_ERROR",
        "message": "The operation could not be completed.",
        "retryable": False,
    }
