"""Database constraints checked by bypassing the typed Python admission path."""

from datetime import date
from uuid import uuid4

import pytest
from psycopg import errors

from chronicle import store
from tests.postgres.test_transactions import batch_rows


@pytest.mark.parametrize(
    "value",
    [
        "NaN",
        "Infinity",
        "1e2",
        "1\n",
        "1 ",
        ".1",
        "0.0000000000001",
        "0" * 129,
        "100000000000000000000000000",
        "--1",
    ],
)
def test_database_rejects_noncontract_lexical_values_without_silent_rounding(db, value):
    store.import_batch(db.importer, db.dataset_id, "B1", batch_rows())
    with pytest.raises(errors.CheckViolation):
        db.importer.execute(
            "INSERT INTO chronicle.revision "
            "(dataset_id,source_namespace,series_key,observation_month,source_revision_key,"
            "status,value,unit_name,metadata_version) "
            "VALUES (%s,'synthetic','employment','2026-02-01','initial','value',%s,'persons','v1')",
            (db.dataset_id, value),
        )


def test_long_source_value_preserves_lexical_precision_at_the_database_boundary(db):
    store.import_batch(db.importer, db.dataset_id, "B1", batch_rows())
    value = "+20000000000000100000000000.000000000003"
    db.importer.execute(
        "INSERT INTO chronicle.revision "
        "(dataset_id,source_namespace,series_key,observation_month,source_revision_key,"
        "status,value,unit_name,metadata_version) "
        "VALUES (%s,'synthetic','employment','2026-02-01','initial','value',%s,'persons','v1')",
        (db.dataset_id, value),
    )
    assert (
        db.reader.execute(
            "SELECT value FROM chronicle.revision "
            "WHERE dataset_id=%s AND observation_month='2026-02-01'",
            (db.dataset_id,),
        ).fetchone()[0]
        == value
    )


def test_seal_rejects_incomplete_batch_without_changing_state(db):
    db.importer.execute(
        "INSERT INTO chronicle.import_batch(dataset_id,batch_id,content_sha256,"
        "expected_occurrence_count) VALUES (%s,'INCOMPLETE',%s,1)",
        (db.dataset_id, "f" * 64),
    )
    with pytest.raises(errors.CheckViolation, match="BATCH_INCOMPLETE"):
        db.importer.execute("SELECT chronicle.seal_batch(%s,'INCOMPLETE')", (db.dataset_id,))
    assert (
        db.reader.execute(
            "SELECT state FROM chronicle.import_batch "
            "WHERE dataset_id=%s AND batch_id='INCOMPLETE'",
            (db.dataset_id,),
        ).fetchone()[0]
        == "staging"
    )


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"mode": None}, "INVALID_MODE"),
        ({"mode": "source_as_of"}, "CUTOFF_REQUIRED"),
        ({"cutoff": date(2026, 3, 1)}, "UNEXPECTED_CUTOFF"),
        ({"mode": "system_snapshot", "snapshot": None}, "SNAPSHOT_REQUIRED"),
        ({"months": []}, "INVALID_GRID"),
        ({"months": [date(2026, 1, 1), None]}, "INVALID_GRID"),
        ({"months": [date(2026, 1, 1), date(2026, 1, 1)]}, "INVALID_GRID"),
        ({"months": [date(2026, 1, 2)]}, "INVALID_GRID"),
        ({"unit": "persons", "metadata": None}, "INVALID_UNIT"),
    ],
)
def test_protected_context_entrypoint_validates_direct_sql_arguments(db, patch, message):
    store.publish_snapshot(db.publisher, db.dataset_id, "EMPTY", [], "0" * 64, None)
    request = {
        "mode": "latest_corrected",
        "snapshot": "EMPTY",
        "months": [date(2026, 1, 1)],
        "cutoff": None,
        "unit": None,
        "metadata": None,
    } | patch
    with pytest.raises(errors.RaiseException, match=message):
        db.reader.execute(
            "SELECT chronicle.create_context(%s,%s,%s,%s,'synthetic','employment',%s,%s,%s,%s)",
            (
                uuid4(),
                db.dataset_id,
                request["snapshot"],
                request["mode"],
                request["months"],
                request["cutoff"],
                request["unit"],
                request["metadata"],
            ),
        )


def test_composite_foreign_keys_prevent_cross_dataset_occurrence(db):
    store.import_batch(db.importer, db.dataset_id, "B1", batch_rows())
    other = f"other-{uuid4().hex}"
    store.create_dataset(db.importer, other)
    db.importer.execute(
        "INSERT INTO chronicle.import_batch(dataset_id,batch_id,content_sha256,"
        "expected_occurrence_count) VALUES (%s,'OPEN',%s,1)",
        (other, "f" * 64),
    )
    with pytest.raises(errors.ForeignKeyViolation):
        db.importer.execute(
            "INSERT INTO chronicle.revision_occurrence "
            "(dataset_id,batch_id,source_namespace,series_key,observation_month,"
            "source_revision_key,received_at) "
            "VALUES (%s,'OPEN','synthetic','employment','2026-01-01','initial',now())",
            (other,),
        )


def test_mixed_source_order_coordinate_systems_are_rejected_by_fk(db):
    store.import_batch(db.importer, db.dataset_id, "B1", batch_rows())
    with pytest.raises(errors.ForeignKeyViolation):
        db.importer.execute(
            "INSERT INTO chronicle.source_release "
            "(dataset_id,source_namespace,source_revision_key,source_order,order_rule,"
            "availability_precision,release_day) "
            "VALUES (%s,'synthetic','incomparable',%s,'date_ordinal','date','2026-04-01')",
            (db.dataset_id, date(2026, 4, 1).toordinal()),
        )
