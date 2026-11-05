"""Privilege and immutable-snapshot checks using actual unprivileged connections."""

import json
from pathlib import Path

import pytest
from psycopg import errors, sql

from chronicle import store
from chronicle.errors import ContractError
from tests.postgres.conftest import import_fixture
from tests.support import query_from_fixture

G1 = json.loads((Path(__file__).parents[1] / "fixtures" / "golden" / "G1.json").read_text())
IMMUTABLE_TABLES = (
    "dataset",
    "source_namespace",
    "series",
    "source_release",
    "revision",
    "import_batch",
    "revision_occurrence",
    "snapshot",
    "snapshot_batch",
    "analysis_context",
)


@pytest.mark.parametrize("role", ["importer", "publisher", "reader"])
@pytest.mark.parametrize("operation", ["update", "delete", "truncate"])
def test_runtime_roles_cannot_rewrite_persisted_evidence(db, role, operation):
    import_fixture(db, G1)
    context_id = store.create_context(
        db.reader, db.dataset_id, query_from_fixture(G1["queries"][0])
    )
    connection = getattr(db, role)
    for table in IMMUTABLE_TABLES:
        target = sql.Identifier("chronicle", table)
        if operation == "update":
            statement = sql.SQL("UPDATE {} SET dataset_id = dataset_id").format(target)
        elif operation == "delete":
            statement = sql.SQL("DELETE FROM {}").format(target)
        else:
            statement = sql.SQL("TRUNCATE {}").format(target)
        with pytest.raises(errors.InsufficientPrivilege):
            with connection.transaction(force_rollback=True):
                connection.execute(statement)
    assert store.select_context(db.reader, context_id) == G1["queries"][0]["expected"]


@pytest.mark.parametrize("role", ["importer", "reader"])
def test_raw_snapshot_membership_and_context_inserts_are_denied(db, role):
    import_fixture(db, G1)
    connection = getattr(db, role)
    for table in ("snapshot", "snapshot_batch", "analysis_context"):
        target = sql.Identifier("chronicle", table)
        statement = sql.SQL("INSERT INTO {} SELECT * FROM {} WHERE false").format(target, target)
        with pytest.raises(errors.InsufficientPrivilege):
            with connection.transaction(force_rollback=True):
                connection.execute(statement)


def test_reader_cannot_insert_sources_or_escalate_to_owner(db):
    import_fixture(db, G1)
    for table in ("dataset", "source_release", "revision", "import_batch", "revision_occurrence"):
        target = sql.Identifier("chronicle", table)
        statement = sql.SQL("INSERT INTO {} SELECT * FROM {} WHERE false").format(target, target)
        with pytest.raises(errors.InsufficientPrivilege):
            with db.reader.transaction(force_rollback=True):
                db.reader.execute(statement)
    for statement in (
        "SET ROLE chronicle_owner",
        "SET SESSION AUTHORIZATION chronicle_owner",
        "CREATE FUNCTION chronicle.forbidden_escalation() RETURNS integer "
        "LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'",
    ):
        with pytest.raises(errors.InsufficientPrivilege):
            with db.reader.transaction(force_rollback=True):
                db.reader.execute(statement)
    with pytest.raises(errors.InsufficientPrivilege):
        db.reader.execute("SELECT chronicle.seal_batch(%s, %s)", (db.dataset_id, "B1"))
    with pytest.raises(errors.InsufficientPrivilege):
        store.publish_snapshot(db.reader, db.dataset_id, "FORBIDDEN", ("B1",), "a" * 64, "S2")
    assert db.reader.execute("SELECT current_user").fetchone()[0] == "chronicle_reader"


def test_definer_functions_have_fixed_search_path_and_only_intended_execute_grants(db):
    definitions = db.reader.execute(
        "SELECT p.proname, p.proconfig, pg_get_userbyid(p.proowner), "
        "EXISTS (SELECT 1 FROM aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) "
        "AS acl WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'), "
        "has_function_privilege('chronicle_importer', p.oid, 'EXECUTE'), "
        "has_function_privilege('chronicle_publisher', p.oid, 'EXECUTE'), "
        "has_function_privilege('chronicle_reader', p.oid, 'EXECUTE') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'chronicle' AND p.prosecdef"
    ).fetchall()
    intended = {
        "guard_batch_occurrence": (False, False, False),
        "seal_batch": (True, False, False),
        "publish_snapshot": (False, True, False),
        "create_context": (False, False, True),
        "publish_snapshot_legacy": (False, False, False),
        "open_object_ticket": (False, True, False),
        "register_object": (False, True, False),
        "object_ready": (False, True, False),
        "end_object_ticket": (False, True, False),
        "attach_batch_artifact": (False, True, False),
        "publish_artifact_snapshot": (False, True, False),
        "revoke_rights": (False, True, False),
        "begin_object_deletion": (False, True, False),
        "finish_object_deletion": (False, True, False),
        "pin_snapshot": (False, True, True),
        "release_snapshot_pin": (False, True, True),
        "claim_ingestion": (False, True, False),
        "record_ingestion_acquisition": (False, True, False),
    }
    assert {row[0] for row in definitions} == set(intended)
    for name, settings, owner, public_execute, *runtime_execute in definitions:
        assert settings is not None and "search_path=pg_catalog" in settings
        assert owner == "chronicle_owner"
        assert public_execute is False
        assert tuple(runtime_execute) == intended[name]


def test_owner_is_nologin_and_runtime_roles_have_no_administrative_attributes(db):
    roles = db.reader.execute(
        "SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls "
        "FROM pg_roles WHERE rolname = ANY(%s)",
        (["chronicle_owner", "chronicle_importer", "chronicle_publisher", "chronicle_reader"],),
    ).fetchall()
    assert len(roles) == 4
    for name, can_login, *administrative in roles:
        assert can_login is (name != "chronicle_owner")
        assert administrative == [False, False, False, False]


def test_staged_batch_cannot_enter_a_published_snapshot(db):
    import_fixture(db, G1)
    context_id = store.create_context(
        db.reader, db.dataset_id, query_from_fixture(G1["queries"][3])
    )
    db.importer.execute(
        "INSERT INTO chronicle.import_batch "
        "(dataset_id, batch_id, content_sha256, expected_occurrence_count) "
        "VALUES (%s, 'STAGED', %s, 1)",
        (db.dataset_id, "b" * 64),
    )
    db.importer.execute(
        "INSERT INTO chronicle.source_release "
        "(dataset_id, source_namespace, source_revision_key, source_order, order_rule, "
        "availability_precision, release_day) "
        "VALUES (%s, 'synthetic', 'staged-release', 30, 'documented_sequence', "
        "'date', '2026-04-15')",
        (db.dataset_id,),
    )
    db.importer.execute(
        "INSERT INTO chronicle.revision "
        "(dataset_id, source_namespace, series_key, observation_month, source_revision_key, "
        "status, value, unit_name, metadata_version) "
        "VALUES (%s, 'synthetic', 'employment', '2026-01-01', 'staged-release', "
        "'value', '999', 'thousand_persons', 'v1')",
        (db.dataset_id,),
    )
    db.importer.execute(
        "INSERT INTO chronicle.revision_occurrence "
        "(dataset_id, batch_id, source_namespace, series_key, observation_month, "
        "source_revision_key, received_at) "
        "VALUES (%s, 'STAGED', 'synthetic', 'employment', '2026-01-01', 'staged-release', "
        "'2026-04-15T10:00:00Z')",
        (db.dataset_id,),
    )
    assert store.select_context(db.reader, context_id) == G1["queries"][3]["expected"]
    with pytest.raises(ContractError, match="UNVALIDATED_SNAPSHOT_BATCH"):
        store.publish_snapshot(
            db.publisher, db.dataset_id, "BAD_STAGED", ("B1", "B2", "STAGED"), "c" * 64, "S2"
        )
    assert (
        db.reader.execute(
            "SELECT count(*) FROM chronicle.snapshot WHERE dataset_id = %s AND snapshot_id = %s",
            (db.dataset_id, "BAD_STAGED"),
        ).fetchone()[0]
        == 0
    )
    assert (
        db.reader.execute(
            "SELECT current_snapshot_id FROM chronicle.dataset WHERE dataset_id = %s",
            (db.dataset_id,),
        ).fetchone()[0]
        == "S2"
    )
    assert store.select_context(db.reader, context_id) == G1["queries"][3]["expected"]


def test_validated_batch_rejects_late_occurrence_before_it_can_change_a_pin(db):
    import_fixture(db, G1)
    context_id = store.create_context(
        db.reader, db.dataset_id, query_from_fixture(G1["queries"][2])
    )
    initial_count = db.reader.execute(
        "SELECT count(*) FROM chronicle.revision_occurrence WHERE dataset_id = %s", (db.dataset_id,)
    ).fetchone()[0]
    # Attempt to attach the already-known 103 revision to old snapshot S1's B1.
    # A fresh receipt instant avoids a uniqueness failure masking the sealed-batch guard.
    with pytest.raises(errors.ObjectNotInPrerequisiteState, match="BATCH_SEALED"):
        db.importer.execute(
            "INSERT INTO chronicle.revision_occurrence "
            "(dataset_id, batch_id, source_namespace, series_key, observation_month, "
            "source_revision_key, received_at) "
            "SELECT dataset_id, 'B1', source_namespace, series_key, observation_month, "
            "source_revision_key, received_at + interval '1 second' "
            "FROM chronicle.revision_occurrence "
            "WHERE dataset_id = %s AND batch_id = 'B2'",
            (db.dataset_id,),
        )
    assert (
        db.reader.execute(
            "SELECT count(*) FROM chronicle.revision_occurrence WHERE dataset_id = %s",
            (db.dataset_id,),
        ).fetchone()[0]
        == initial_count
    )
    assert store.select_context(db.reader, context_id) == G1["queries"][2]["expected"]
