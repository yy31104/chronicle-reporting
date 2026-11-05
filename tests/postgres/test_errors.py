"""Real diagnostic identities, transaction rollback and retryable failure classes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from psycopg import errors, sql

from chronicle import store
from chronicle.domain import Occurrence
from chronicle.errors import (
    ContractError,
    RetryableTransactionError,
    database_errors,
    public_error,
)
from tests.postgres.test_transactions import batch_rows


def count_batch(db, batch):
    return db.reader.execute(
        "SELECT count(*) FROM chronicle.import_batch WHERE dataset_id=%s AND batch_id=%s",
        (db.dataset_id, batch),
    ).fetchone()[0]


@pytest.mark.parametrize("previously_committed", [False, True])
def test_ambiguity_has_one_business_code_and_no_partial_batch(db, previously_committed):
    first = batch_rows()[0]
    second = replace(first, source_revision_key="other-release", value="999")
    if previously_committed:
        store.import_batch(db.importer, db.dataset_id, "B1", (first,))
        second = replace(second, occurrences=(Occurrence("BAD", datetime(2026, 4, 1, tzinfo=UTC)),))
        batch, incoming = "BAD", (second,)
    else:
        batch, incoming = "B1", (first, second)
    with pytest.raises(ContractError) as caught:
        store.import_batch(db.importer, db.dataset_id, batch, incoming)
    assert caught.value.code == "AMBIGUOUS_SOURCE_ORDER"
    assert count_batch(db, batch) == 0
    assert db.reader.execute(
        "SELECT count(*) FROM chronicle.source_release WHERE dataset_id=%s",
        (db.dataset_id,),
    ).fetchone()[0] == int(previously_committed)
    assert db.importer.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize("batch", ["", " bad", "x" * 201])
def test_invalid_batch_check_is_translated_after_complete_rollback(db, batch):
    with pytest.raises(ContractError) as caught:
        store.import_batch(db.importer, db.dataset_id, batch, ())
    assert caught.value.code == "INVALID_IDENTITY"
    assert count_batch(db, batch) == 0


def test_missing_dataset_foreign_key_is_a_typed_rejection(db):
    with pytest.raises(ContractError) as caught:
        store.import_batch(db.importer, "absent-" + uuid4().hex, "B1", batch_rows())
    assert caught.value.code == "UNKNOWN_DATASET"
    assert db.importer.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("content_sha256", "not-a-hash", "INVALID_MANIFEST_HASH"),
        ("expected_occurrence_count", -1, "INVALID_OCCURRENCE_COUNT"),
    ],
)
def test_known_checks_use_structured_diagnostics(db, field, value, code):
    content = {"content_sha256": "a" * 64, "expected_occurrence_count": 0, field: value}
    with pytest.raises(ContractError) as caught:
        with database_errors(), db.importer.transaction():
            db.importer.execute(
                "INSERT INTO chronicle.import_batch "
                "(dataset_id,batch_id,content_sha256,expected_occurrence_count) "
                "VALUES (%s,'BAD',%s,%s)",
                (db.dataset_id, content["content_sha256"], content["expected_occurrence_count"]),
            )
    assert caught.value.code == code
    assert count_batch(db, "BAD") == 0


def test_conflicting_revision_still_has_typed_identity_error(db):
    first = batch_rows()[0]
    store.import_batch(db.importer, db.dataset_id, "B1", (first,))
    changed = replace(
        first, value="999", occurrences=(Occurrence("BAD", datetime(2026, 4, 1, tzinfo=UTC)),)
    )
    with pytest.raises(ContractError) as caught:
        store.import_batch(db.importer, db.dataset_id, "BAD", (changed,))
    assert caught.value.code == "REVISION_CONFLICT"
    assert count_batch(db, "BAD") == 0


def test_structured_function_error_is_translated_without_message_parsing(db):
    with pytest.raises(ContractError) as caught:
        store.publish_snapshot(db.publisher, db.dataset_id, "S1", [], "bad", None)
    assert caught.value.code == "INVALID_MANIFEST_HASH"
    # Change only the localized message, keeping the structured diagnostic identity.
    with pytest.raises(ContractError) as caught:
        with database_errors():
            db.admin.execute("""DO $$ BEGIN RAISE EXCEPTION USING
                ERRCODE='P0001', MESSAGE='Unrelated message text', SCHEMA='chronicle',
                TABLE='snapshot', CONSTRAINT='api_invalid_manifest_hash'; END $$""")
    assert caught.value.code == "INVALID_MANIFEST_HASH"


@pytest.mark.parametrize(
    "schema,table,constraint",
    [
        ("public", "source_release", "source_release_order_unique"),
        ("chronicle", "other_table", "source_release_order_unique"),
        ("chronicle", "source_release", "unknown_constraint"),
    ],
)
def test_similarly_named_or_unknown_constraint_is_not_business_rejection(
    db, schema, table, constraint
):
    statement = sql.SQL("""DO $$ BEGIN RAISE EXCEPTION USING ERRCODE='23505',
        MESSAGE='AMBIGUOUS_SOURCE_ORDER with private diagnostic',
        SCHEMA={}, TABLE={}, CONSTRAINT={}; END $$""").format(
        sql.Literal(schema), sql.Literal(table), sql.Literal(constraint)
    )
    with pytest.raises(errors.UniqueViolation) as caught:
        with database_errors():
            db.admin.execute(statement)
    assert public_error(caught.value) == {
        "code": "DATABASE_ERROR",
        "message": "The database operation failed.",
        "retryable": False,
    }


def test_unrecognized_database_failure_is_not_input_rejection_and_does_not_leak(db):
    with pytest.raises(errors.UndefinedTable) as caught:
        with database_errors(), db.importer.transaction():
            db.importer.execute("SELECT * FROM chronicle.not_an_actual_secret_table")
    envelope = public_error(caught.value)
    assert envelope["code"] == "DATABASE_ERROR"
    assert "secret" not in str(envelope)
    assert "SELECT" not in str(envelope)
    assert db.importer.execute("SELECT 1").fetchone() == (1,)
    assert public_error(ContractError("INVALID_IDENTITY", "password=secret")) == {
        "code": "INVALID_IDENTITY",
        "message": "The request violates Chronicle's contract.",
        "retryable": False,
    }
    assert "secret" not in str(public_error(RuntimeError("password=secret")))


@pytest.fixture
def mutable_probe(db):
    """Independent disposable rows for genuine PostgreSQL transaction failures."""
    name = sql.Identifier("public", "error_probe_" + uuid4().hex)
    db.admin.execute(sql.SQL("CREATE TABLE {}(id int PRIMARY KEY,value int NOT NULL)").format(name))
    db.admin.execute(sql.SQL("INSERT INTO {} VALUES (1,0),(2,0)").format(name))
    try:
        yield name
    finally:
        db.admin.execute(sql.SQL("DROP TABLE {}").format(name))


def test_real_serialization_failure_requires_complete_retry(db, mutable_probe):
    first, second = db.connect("admin"), db.connect("admin")
    select = sql.SQL("SELECT sum(value) FROM {}").format(mutable_probe)
    update = sql.SQL("UPDATE {} SET value=value+1 WHERE id=%s").format(mutable_probe)
    with pytest.raises(RetryableTransactionError) as caught:
        with database_errors(), second.transaction():
            second.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            assert second.execute(select).fetchone() == (0,)
            with first.transaction():
                first.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                assert first.execute(select).fetchone() == (0,)
                first.execute(update, (1,))
            second.execute(update, (2,))
    assert caught.value.reason == "SERIALIZATION_FAILURE"
    assert public_error(caught.value)["retryable"] is True
    assert db.admin.execute(select).fetchone() == (1,)
    assert second.execute("SELECT 1").fetchone() == (1,)


def test_real_deadlock_is_retryable_and_victim_rolls_back(db, mutable_probe):
    first, second = db.connect("admin"), db.connect("admin")
    barrier = Barrier(2)
    update = sql.SQL("UPDATE {} SET value=value+1 WHERE id=%s").format(mutable_probe)

    def run(conn, left, right):
        try:
            with database_errors(), conn.transaction():
                conn.execute(update, (left,))
                barrier.wait(timeout=5)
                conn.execute(update, (right,))
        except RetryableTransactionError as exc:
            assert exc.reason == "DEADLOCK_DETECTED"
            return public_error(exc)["code"]
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(run, first, 1, 2), pool.submit(run, second, 2, 1)]
        outcomes = [job.result(timeout=10) for job in jobs]
    assert sorted(outcomes) == ["TRANSACTION_RETRY_REQUIRED", "committed"]
    assert db.admin.execute(
        sql.SQL("SELECT id,value FROM {} ORDER BY id").format(mutable_probe)
    ).fetchall() == [(1, 1), (2, 1)]
