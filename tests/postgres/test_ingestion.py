"""Resumable source admission, retained receipt identity and explicit parser repairs."""

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import errors, sql

from chronicle import ingestion, objects, reports, sources, store
from chronicle.domain import Query
from chronicle.errors import ContractError
from tests.postgres.conftest import database as database_fixture

FIXTURES = Path(__file__).parents[1] / "fixtures/sources"
RECEIVED = datetime(2026, 10, 29, 13, 18, 9, tzinfo=UTC)
FIRST, SECOND = "frb-2024-02-15", "frb-2024-03-15"


@pytest.fixture
def database():
    # Each object root must describe its whole catalog, not just one dataset.
    yield from database_fixture.__wrapped__()


@pytest.fixture
def source_environment(db, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("CHRONICLE_OBJECT_ROOT", str(tmp_path))

    def fetch(source_id, batch):
        calls.append(source_id)
        artifact = sources.parse_payload(
            source_id, (FIXTURES / (source_id + ".html")).read_bytes(), RECEIVED, batch
        )
        # Revocation is intentionally policy-wide. Give each isolated test's
        # synthetic acquisition its own policy identity content.
        artifact.manifest["rights_policy"]["test_scope"] = db.dataset_id
        return artifact

    monkeypatch.setattr(sources, "fetch_and_parse", fetch)
    return calls, tmp_path


def import_one(db, key=FIRST, snapshot="S1", parent=None, request=None, fault=None):
    return ingestion.import_sources(
        db.importer,
        db.publisher,
        db.dataset_id,
        snapshot,
        (key,),
        parent,
        request or uuid4(),
        _fault=fault,
    )


def mean_report(db, dataset, snapshot):
    context = store.create_context(
        db.reader,
        dataset,
        Query("system_snapshot", snapshot, "frb.g17.archive", "production", (date(2024, 1, 1),)),
    )
    return reports.build_report(db.reader, context, "production_mean3")


def test_end_to_end_import_retains_closure_and_cumulative_old_report(db, source_environment):
    calls, root = source_environment
    request = uuid4()
    assert import_one(db, request=request) == "S1"
    old = mean_report(db, db.dataset_id, "S1")
    assert old["rows"][0]["value"] == "102.666666666667"
    import_one(db, SECOND, "S2", "S1")
    assert mean_report(db, db.dataset_id, "S2")["rows"][0]["value"] == "102.633333333333"
    assert import_one(db, request=request) == "S1"
    assert calls == [FIRST, SECOND]
    assert reports.replay_report(db.reader, UUID_from(old["report_id"])) == old
    assert objects.verify_closure(db.reader, root=root) == []
    assert db.reader.execute(
        "SELECT current_snapshot_id FROM chronicle.dataset WHERE dataset_id=%s", (db.dataset_id,)
    ).fetchone() == ("S2",)
    assert db.reader.execute(
        "SELECT count(*) FROM chronicle.snapshot_batch WHERE dataset_id=%s AND snapshot_id='S2'",
        (db.dataset_id,),
    ).fetchone() == (2,)


def test_reobserved_release_keeps_distinct_bytes_receipts_and_deduplicates_revision(
    db, source_environment, monkeypatch
):
    """Publisher HTML can change outside reviewed cells without a source revision."""
    request = uuid4()
    import_one(db, request=request)
    original_report = mean_report(db, db.dataset_id, "S1")
    later_receipt = RECEIVED + timedelta(hours=1)
    original_bytes = (FIXTURES / (FIRST + ".html")).read_bytes()
    changed_bytes = original_bytes.replace(b"</body>", b"<!-- navigation build 2 --></body>")
    assert changed_bytes != original_bytes

    def refetch(key, batch):
        assert key == FIRST
        artifact = sources.parse_payload(key, changed_bytes, later_receipt, batch)
        artifact.manifest["rights_policy"]["test_scope"] = db.dataset_id
        return artifact

    monkeypatch.setattr(sources, "fetch_and_parse", refetch)
    second_request = uuid4()
    assert import_one(db, FIRST, "S2", "S1", second_request) == "S2"
    assert mean_report(db, db.dataset_id, "S2")["rows"][0]["value"] == "102.666666666667"
    assert (
        reports.replay_report(db.reader, UUID_from(original_report["report_id"])) == original_report
    )
    assert db.reader.execute(
        "SELECT count(*) FROM chronicle.revision WHERE dataset_id=%s", (db.dataset_id,)
    ).fetchone() == (3,)
    assert db.reader.execute(
        "SELECT received_at,count(*) FROM chronicle.revision_occurrence WHERE dataset_id=%s "
        "GROUP BY received_at ORDER BY received_at",
        (db.dataset_id,),
    ).fetchall() == [(RECEIVED, 3), (later_receipt, 3)]
    retained = db.reader.execute(
        "SELECT object_sha256,source_metadata->>'received_at' FROM chronicle.batch_artifact "
        "WHERE dataset_id=%s ORDER BY source_metadata->>'received_at'",
        (db.dataset_id,),
    ).fetchall()
    assert len(retained) == 2 and retained[0][0] != retained[1][0]
    assert [row[1] for row in retained] == [RECEIVED.isoformat(), later_receipt.isoformat()]
    monkeypatch.setattr(sources, "fetch_and_parse", lambda *_: pytest.fail("Replay must not fetch"))
    assert import_one(db, FIRST, "S2", "S1", second_request) == "S2"


def test_saved_source_request_binds_bytes_and_reuses_original_local_receipt(
    db, source_environment, monkeypatch, tmp_path
):
    path = tmp_path / "official-save.html"
    path.write_bytes((FIXTURES / (FIRST + ".html")).read_bytes())
    request = uuid4()
    monkeypatch.setattr(
        sources, "fetch_and_parse", lambda *_: pytest.fail("Saved source must not fetch")
    )

    def run(fault=None):
        return ingestion.import_sources(
            db.importer,
            db.publisher,
            db.dataset_id,
            "S1",
            (FIRST,),
            None,
            request,
            local_files={FIRST: path},
            _fault=fault,
        )

    def stop(phase):
        if phase == "acquisition_retained":
            raise RuntimeError("injected saved-source stop")

    with pytest.raises(RuntimeError, match="saved-source stop"):
        run(stop)
    initial = db.reader.execute(
        "SELECT source_document->'manifest'->>'received_at' FROM chronicle.ingestion_acquisition "
        "WHERE request_id=%s",
        (request,),
    ).fetchone()[0]
    assert run() == "S1"
    assert db.reader.execute(
        "SELECT DISTINCT received_at FROM chronicle.revision_occurrence WHERE dataset_id=%s",
        (db.dataset_id,),
    ).fetchall() == [(datetime.fromisoformat(initial),)]
    repaired_dataset = db.dataset_id + "-repair"
    assert (
        ingestion.repair_generation(
            db.importer,
            db.publisher,
            db.dataset_id,
            "S1",
            repaired_dataset,
            "R1",
            "Recheck the saved source interpretation while preserving acquisition provenance",
            uuid4(),
        )
        == "R1"
    )
    repaired_metadata = db.reader.execute(
        "SELECT source_metadata FROM chronicle.batch_artifact WHERE dataset_id=%s",
        (repaired_dataset,),
    ).fetchone()[0]
    assert repaired_metadata["acquisition_channel"] == "operator_supplied_direct_publisher_html"
    assert repaired_metadata["acquisition"]["authenticity"] == (
        "operator_asserted_not_independently_authenticated"
    )
    assert repaired_metadata["received_at"] == initial
    path.write_bytes(path.read_bytes() + b"<!-- changed saved bytes -->")
    with pytest.raises(ContractError) as caught:
        run()
    assert caught.value.code == "INGESTION_IDENTITY_CONFLICT"


def UUID_from(value):
    from uuid import UUID

    return UUID(str(value))


@pytest.mark.parametrize(
    "phase", ["acquisition_retained", "batch_committed", "publication_committed"]
)
def test_crash_resume_reuses_original_bytes_rows_receipt_and_request(db, source_environment, phase):
    calls, _ = source_environment
    request = uuid4()

    def fail(point):
        if point == phase:
            raise RuntimeError("injected stop")

    with pytest.raises(RuntimeError, match="injected stop"):
        import_one(db, request=request, fault=fail)
    assert import_one(db, request=request) == "S1"
    assert calls == [FIRST]
    assert db.reader.execute(
        "SELECT DISTINCT received_at FROM chronicle.revision_occurrence WHERE dataset_id=%s",
        (db.dataset_id,),
    ).fetchall() == [(RECEIVED,)]
    assert db.reader.execute(
        "SELECT count(*) FROM chronicle.staging_ticket WHERE dataset_id=%s AND state='active'",
        (db.dataset_id,),
    ).fetchone() == (0,)


def test_changed_request_rejected_and_completed_identity_survives_parser_upgrade(
    db, source_environment, monkeypatch
):
    calls, _ = source_environment
    request = uuid4()
    import_one(db, request=request)
    with pytest.raises(ContractError) as caught:
        import_one(db, SECOND, request=request)
    assert caught.value.code == "INGESTION_IDENTITY_CONFLICT"
    monkeypatch.setattr(sources, "PARSER_VERSION", "next-parser-version")
    assert import_one(db, request=request) == "S1"
    with pytest.raises(ContractError) as caught:
        import_one(db, SECOND, "S2", "S1")
    assert caught.value.code == "PARSER_GENERATION_REQUIRED"
    assert calls == [FIRST]


def test_partial_fetch_failure_does_not_publish_or_substitute(db, source_environment, monkeypatch):
    fetch = sources.fetch_and_parse

    def partial(key, batch):
        if key == SECOND:
            raise ContractError("SOURCE_UNAVAILABLE", "Publisher denied request")
        return fetch(key, batch)

    monkeypatch.setattr(sources, "fetch_and_parse", partial)
    request = uuid4()
    with pytest.raises(ContractError) as caught:
        ingestion.import_sources(
            db.importer, db.publisher, db.dataset_id, "S1", (FIRST, SECOND), None, request
        )
    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert db.reader.execute(
        "SELECT current_snapshot_id FROM chronicle.dataset WHERE dataset_id=%s", (db.dataset_id,)
    ).fetchone() == (None,)
    assert db.reader.execute(
        "SELECT count(*) FROM chronicle.snapshot WHERE dataset_id=%s", (db.dataset_id,)
    ).fetchone() == (0,)
    monkeypatch.setattr(sources, "fetch_and_parse", fetch)
    assert (
        ingestion.import_sources(
            db.importer, db.publisher, db.dataset_id, "S1", (FIRST, SECOND), None, request
        )
        == "S1"
    )
    assert source_environment[0] == [FIRST, SECOND]


def test_concurrent_same_request_acquires_once(db, source_environment):
    request = uuid4()
    connections = [(db.connect("importer"), db.connect("publisher")) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [
            pool.submit(
                ingestion.import_sources, i, p, db.dataset_id, "S1", (FIRST,), None, request
            )
            for i, p in connections
        ]
        assert [job.result(timeout=10) for job in jobs] == ["S1", "S1"]
    assert source_environment[0] == [FIRST]


def test_parser_column_repair_creates_generation_preserving_original_report(
    db, source_environment, monkeypatch
):
    real_fetch = sources.fetch_and_parse
    original_version = sources.PARSER_VERSION

    def bad_column(key, batch):
        artifact = real_fetch(key, batch)
        # A deliberately wrong previous implementation takes a percent-change
        # column as an index level. Raw publisher HTML remains the correct bytes.
        manifest = copy.deepcopy(artifact.manifest)
        manifest["cells"][0].update(value="0.3", raw_text=".3")
        revisions = (replace(artifact.revisions[0], value="0.3"), *artifact.revisions[1:])
        return sources.SourceArtifact(artifact.raw, revisions, manifest)

    monkeypatch.setattr(sources, "PARSER_VERSION", "broken-column-parser-v0")
    monkeypatch.setattr(sources, "fetch_and_parse", bad_column)
    import_one(db)
    old = mean_report(db, db.dataset_id, "S1")
    assert old["rows"][0]["value"] == "68.533333333333"
    monkeypatch.setattr(sources, "PARSER_VERSION", original_version)
    monkeypatch.setattr(
        sources, "fetch_and_parse", lambda *_: pytest.fail("Repair must not refetch")
    )
    repaired = db.dataset_id + "-repaired"
    request = uuid4()
    assert (
        ingestion.repair_generation(
            db.importer,
            db.publisher,
            db.dataset_id,
            "S1",
            repaired,
            "S1",
            "Old parser selected percent-change column as index level",
            request,
        )
        == "S1"
    )
    assert mean_report(db, repaired, "S1")["rows"][0]["value"] == "102.666666666667"
    assert reports.replay_report(db.reader, UUID_from(old["report_id"])) == old
    record = db.reader.execute(
        "SELECT predecessor_dataset,predecessor_snapshot,defect_note,interpreted_at "
        "FROM chronicle.interpretation_generation WHERE dataset_id=%s",
        (repaired,),
    ).fetchone()
    assert record[:3] == (
        db.dataset_id,
        "S1",
        "Old parser selected percent-change column as index level",
    )
    assert record[3] > RECEIVED
    manifest = db.reader.execute(
        "SELECT source_metadata FROM chronicle.batch_artifact WHERE dataset_id=%s", (repaired,)
    ).fetchone()[0]
    assert manifest["received_at"] == RECEIVED.isoformat()
    assert manifest["interpretation"]["original_received_at"] == RECEIVED.isoformat()
    assert manifest["interpretation"]["interpreted_at"] == record[3].isoformat()
    assert (
        ingestion.repair_generation(
            db.importer,
            db.publisher,
            db.dataset_id,
            "S1",
            repaired,
            "S1",
            "Old parser selected percent-change column as index level",
            request,
        )
        == "S1"
    )


@pytest.mark.parametrize("failure", ["corrupt", "revoked"])
def test_repair_checks_current_rights_and_original_bytes_before_parsing(
    db, source_environment, monkeypatch, failure
):
    import_one(db)
    root = source_environment[1]
    digest, policy = db.reader.execute(
        "SELECT object_sha256,policy_sha256 FROM chronicle.batch_artifact WHERE dataset_id=%s",
        (db.dataset_id,),
    ).fetchone()
    if failure == "corrupt":
        objects.object_path(digest, root).write_bytes(b"corrupt")
    else:
        objects.revoke_rights(db.publisher, policy, "test revoked")
    monkeypatch.setattr(
        sources, "parse_payload", lambda *_a, **_k: pytest.fail("Must verify before parse")
    )
    with pytest.raises(ContractError) as caught:
        ingestion.repair_generation(
            db.importer,
            db.publisher,
            db.dataset_id,
            "S1",
            db.dataset_id + "-r",
            "S1",
            "Repair rejected input",
            uuid4(),
        )
    assert caught.value.code == (
        "RIGHTS_DENIED" if failure == "revoked" else "SNAPSHOT_EVIDENCE_UNAVAILABLE"
    )


@pytest.mark.parametrize(
    "table", ["ingestion_request", "interpretation_generation", "ingestion_acquisition"]
)
def test_ingestion_lineage_is_immutable_even_to_owner(db, source_environment, table):
    import_one(db)
    with pytest.raises(errors.ObjectNotInPrerequisiteState) as caught:
        with db.admin.transaction():
            db.admin.execute("SET LOCAL ROLE chronicle_owner")
            db.admin.execute(sql.SQL("DELETE FROM chronicle.{}").format(sql.Identifier(table)))
    assert caught.value.sqlstate == "55000"


@pytest.mark.parametrize("source_id", ["bls-2024-02-02", "frb-2024-02-15"])
def test_packaged_policy_hash_reaches_database_artifact_and_manifest(
    db, monkeypatch, tmp_path, source_id
):
    import hashlib
    import json

    monkeypatch.setenv("CHRONICLE_OBJECT_ROOT", str(tmp_path))
    suffix = "-modeled" if source_id.startswith("bls-") else ""
    saved = FIXTURES / (source_id + suffix + ".html")
    ingestion.import_sources(
        db.importer,
        db.publisher,
        db.dataset_id,
        "policy-snapshot",
        (source_id,),
        None,
        uuid4(),
        local_files={source_id: saved},
    )
    inventory = json.loads((Path(__file__).parents[2] / "data/resource-manifest.json").read_text())
    expected = inventory["policy_sha256"][source_id[:3]]
    policy_hash, policy = db.reader.execute(
        "SELECT a.policy_sha256,p.policy FROM chronicle.batch_artifact a "
        "JOIN chronicle.rights_policy p USING(policy_sha256) WHERE a.dataset_id=%s",
        (db.dataset_id,),
    ).fetchone()
    assert policy["reviewer"] == "project maintainer"
    assert (
        hashlib.sha256(
            json.dumps(
                policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode()
        ).hexdigest()
        == policy_hash
        == expected
    )
    row = db.reader.execute(
        "SELECT manifest_sha256 FROM chronicle.artifact_publication WHERE dataset_id=%s",
        (db.dataset_id,),
    ).fetchone()
    manifest = json.loads(
        objects.read_object(db.reader, db.dataset_id, "policy-snapshot", row[0], root=tmp_path)
    )
    assert {item["policy_sha256"] for item in manifest["artifacts"]} == {expected}
