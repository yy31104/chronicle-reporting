-- C1-PR03: stable, structured database diagnostics; no data or behavior rewrite.
-- Rename existing constraints without rebuilding them. Function replacements keep
-- SQLSTATE and message compatibility while adding machine-readable diagnostics.
ALTER TABLE chronicle.source_release RENAME CONSTRAINT source_release_dataset_id_source_namespace_source_order_key TO source_release_order_unique;
ALTER TABLE chronicle.source_release RENAME CONSTRAINT source_release_check TO source_release_availability_consistent;
ALTER TABLE chronicle.source_release RENAME CONSTRAINT source_release_check1 TO source_release_order_matches_date;
ALTER TABLE chronicle.revision RENAME CONSTRAINT revision_check TO revision_value_matches_status;
ALTER TABLE chronicle.analysis_context RENAME CONSTRAINT analysis_context_check TO analysis_context_mode_cutoff_consistent;
ALTER TABLE chronicle.analysis_context RENAME CONSTRAINT analysis_context_check1 TO analysis_context_unit_pair_consistent;

-- Protected, transaction-local publication of complete explicit batch sets.
-- This is C1 database publication; it does not implement the C2 object protocol.

CREATE OR REPLACE FUNCTION chronicle.publish_snapshot(
    p_dataset text,
    p_snapshot text,
    p_batch_ids text[],
    p_manifest_sha256 text,
    p_expected_current text DEFAULT NULL
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_current text;
    v_batches text[];
    v_existing_batches text[];
    v_existing_manifest text;
BEGIN
    IF chronicle.valid_identifier(p_dataset) IS NOT TRUE
        OR chronicle.valid_identifier(p_snapshot) IS NOT TRUE
        OR (p_expected_current IS NOT NULL AND NOT chronicle.valid_identifier(p_expected_current))
    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'INVALID_IDENTITY',
            SCHEMA = 'chronicle', TABLE = 'snapshot', CONSTRAINT = 'api_invalid_identity';
    END IF;
    IF p_manifest_sha256 IS NULL OR p_manifest_sha256 COLLATE "C" !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'INVALID_MANIFEST_HASH',
            SCHEMA = 'chronicle', TABLE = 'snapshot', CONSTRAINT = 'api_invalid_manifest_hash';
    END IF;
    IF p_batch_ids IS NULL
        OR (pg_catalog.cardinality(p_batch_ids) > 0 AND pg_catalog.array_ndims(p_batch_ids) <> 1)
        OR EXISTS (
            SELECT 1 FROM pg_catalog.unnest(p_batch_ids) AS item(batch_id)
            WHERE chronicle.valid_identifier(item.batch_id) IS NOT TRUE
        )
        OR (SELECT pg_catalog.count(*) FROM pg_catalog.unnest(p_batch_ids) AS item(batch_id))
            <> (SELECT pg_catalog.count(DISTINCT item.batch_id)
                FROM pg_catalog.unnest(p_batch_ids) AS item(batch_id))
    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'INVALID_SNAPSHOT_MEMBERSHIP',
            SCHEMA = 'chronicle', TABLE = 'snapshot', CONSTRAINT = 'api_invalid_snapshot_membership';
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(item.batch_id ORDER BY item.batch_id), ARRAY[]::text[])
    INTO v_batches
    FROM pg_catalog.unnest(p_batch_ids) AS item(batch_id);

    -- Serializes publication and compare-and-swap for this dataset only.
    SELECT d.current_snapshot_id INTO v_current
    FROM chronicle.dataset AS d
    WHERE d.dataset_id = p_dataset
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'UNKNOWN_DATASET',
            SCHEMA = 'chronicle', TABLE = 'snapshot', CONSTRAINT = 'api_unknown_dataset';
    END IF;

    SELECT s.fixture_manifest_sha256 INTO v_existing_manifest
    FROM chronicle.snapshot AS s
    WHERE s.dataset_id = p_dataset AND s.snapshot_id = p_snapshot;
    IF FOUND THEN
        SELECT COALESCE(pg_catalog.array_agg(sb.batch_id ORDER BY sb.batch_id), ARRAY[]::text[])
        INTO v_existing_batches
        FROM chronicle.snapshot_batch AS sb
        WHERE sb.dataset_id = p_dataset AND sb.snapshot_id = p_snapshot;
        IF v_existing_manifest IS DISTINCT FROM p_manifest_sha256
            OR v_existing_batches IS DISTINCT FROM v_batches
        THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SNAPSHOT_IDENTITY_CONFLICT',
            SCHEMA = 'chronicle', TABLE = 'snapshot', CONSTRAINT = 'api_snapshot_identity_conflict';
        END IF;
        -- An old successful retry cannot rewind a newer current snapshot.
        RETURN p_snapshot;
    END IF;

    IF v_current IS DISTINCT FROM p_expected_current THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SNAPSHOT_BASE_CHANGED',
            SCHEMA = 'chronicle', TABLE = 'snapshot', CONSTRAINT = 'api_snapshot_base_changed';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(v_batches) AS wanted(batch_id)
        LEFT JOIN chronicle.import_batch AS b
            ON b.dataset_id = p_dataset AND b.batch_id = wanted.batch_id
        WHERE b.batch_id IS NULL OR b.state <> 'validated'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'UNVALIDATED_SNAPSHOT_BATCH',
            SCHEMA = 'chronicle', TABLE = 'snapshot', CONSTRAINT = 'api_unvalidated_snapshot_batch';
    END IF;

    INSERT INTO chronicle.snapshot(dataset_id, snapshot_id, fixture_manifest_sha256)
    VALUES (p_dataset, p_snapshot, p_manifest_sha256);
    INSERT INTO chronicle.snapshot_batch(dataset_id, snapshot_id, batch_id)
    SELECT p_dataset, p_snapshot, item.batch_id
    FROM pg_catalog.unnest(v_batches) AS item(batch_id);
    UPDATE chronicle.dataset SET current_snapshot_id = p_snapshot WHERE dataset_id = p_dataset;
    RETURN p_snapshot;
END;
$$;

REVOKE ALL ON FUNCTION chronicle.publish_snapshot(text, text, text[], text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION chronicle.publish_snapshot(text, text, text[], text, text)
    TO chronicle_publisher;


CREATE OR REPLACE FUNCTION chronicle.create_context(
    p_context_id uuid,
    p_dataset text,
    p_snapshot text,
    p_mode text,
    p_namespace text,
    p_series text,
    p_months date[],
    p_cutoff date,
    p_unit_name text,
    p_metadata_version text
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_snapshot text;
    v_existing chronicle.analysis_context%ROWTYPE;
BEGIN
    IF p_context_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'INVALID_IDENTITY',
            SCHEMA = 'chronicle', TABLE = 'analysis_context', CONSTRAINT = 'api_invalid_identity';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(ARRAY[p_dataset, p_namespace, p_series]) AS item(identifier)
        WHERE chronicle.valid_identifier(item.identifier) IS NOT TRUE
    ) OR (p_snapshot IS NOT NULL AND NOT chronicle.valid_identifier(p_snapshot)) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'INVALID_IDENTITY',
            SCHEMA = 'chronicle', TABLE = 'analysis_context', CONSTRAINT = 'api_invalid_identity';
    END IF;
    IF p_mode IS NULL OR p_mode NOT IN ('latest_corrected', 'source_as_of', 'system_snapshot') THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'INVALID_MODE',
            SCHEMA = 'chronicle', TABLE = 'analysis_context', CONSTRAINT = 'api_invalid_mode';
    END IF;
    IF p_mode = 'system_snapshot' AND p_snapshot IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SNAPSHOT_REQUIRED',
            SCHEMA = 'chronicle', TABLE = 'analysis_context', CONSTRAINT = 'api_snapshot_required';
    END IF;
    IF p_mode = 'source_as_of' AND p_cutoff IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CUTOFF_REQUIRED',
            SCHEMA = 'chronicle', TABLE = 'analysis_context', CONSTRAINT = 'api_cutoff_required';
    END IF;
    IF p_mode <> 'source_as_of' AND p_cutoff IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'UNEXPECTED_CUTOFF',
            SCHEMA = 'chronicle', TABLE = 'analysis_context', CONSTRAINT = 'api_unexpected_cutoff';
    END IF;
    IF p_cutoff IS NOT NULL AND p_cutoff NOT BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'
    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'INVALID_DATE',
            SCHEMA = 'chronicle', TABLE = 'analysis_context', CONSTRAINT = 'api_invalid_date';
    END IF;
    IF chronicle.valid_months(p_months) IS NOT TRUE THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'INVALID_GRID',
            SCHEMA = 'chronicle', TABLE = 'analysis_context', CONSTRAINT = 'api_invalid_grid';
    END IF;
    IF (p_unit_name IS NULL) <> (p_metadata_version IS NULL)
        OR (p_unit_name IS NOT NULL AND (
            NOT chronicle.valid_identifier(p_unit_name)
            OR NOT chronicle.valid_identifier(p_metadata_version)
        ))
    THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'INVALID_UNIT',
            SCHEMA = 'chronicle', TABLE = 'analysis_context', CONSTRAINT = 'api_invalid_unit';
    END IF;

    -- Replay a prior implicit-current request against its original pin, not the
    -- current pointer at retry time. Other context fields must still match.
    SELECT c.* INTO v_existing
    FROM chronicle.analysis_context AS c
    WHERE c.context_id = p_context_id;
    IF FOUND AND p_snapshot IS NULL THEN
        v_snapshot := v_existing.snapshot_id;
    ELSIF p_snapshot IS NOT NULL THEN
        v_snapshot := p_snapshot;
    ELSE
        SELECT d.current_snapshot_id INTO v_snapshot
        FROM chronicle.dataset AS d WHERE d.dataset_id = p_dataset;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'UNKNOWN_DATASET',
            SCHEMA = 'chronicle', TABLE = 'analysis_context', CONSTRAINT = 'api_unknown_dataset';
        END IF;
    END IF;
    IF v_existing.context_id IS NULL THEN
        IF v_snapshot IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'NO_CURRENT_SNAPSHOT',
            SCHEMA = 'chronicle', TABLE = 'analysis_context', CONSTRAINT = 'api_no_current_snapshot';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM chronicle.snapshot AS s
            WHERE s.dataset_id = p_dataset AND s.snapshot_id = v_snapshot
        ) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'UNKNOWN_SNAPSHOT',
            SCHEMA = 'chronicle', TABLE = 'analysis_context', CONSTRAINT = 'api_unknown_snapshot';
        END IF;

        INSERT INTO chronicle.analysis_context(
            context_id, dataset_id, snapshot_id, mode, source_namespace, series_key,
            months, exclusive_end_date, expected_unit_name, expected_metadata_version
        ) VALUES (
            p_context_id, p_dataset, v_snapshot, p_mode, p_namespace, p_series,
            p_months, p_cutoff, p_unit_name, p_metadata_version
        ) ON CONFLICT (context_id) DO NOTHING;
        SELECT c.* INTO v_existing
        FROM chronicle.analysis_context AS c
        WHERE c.context_id = p_context_id;
    END IF;
    -- A concurrent identical request may have committed while this insertion
    -- waited on the UUID conflict. Its stored pin also wins an implicit-current
    -- replay; a newer pointer resolved before that wait is not a new request.
    IF p_snapshot IS NULL THEN
        v_snapshot := v_existing.snapshot_id;
    END IF;
    IF ROW(
        v_existing.dataset_id, v_existing.snapshot_id, v_existing.mode,
        v_existing.source_namespace, v_existing.series_key, v_existing.months,
        v_existing.exclusive_end_date, v_existing.expected_unit_name,
        v_existing.expected_metadata_version
    ) IS DISTINCT FROM ROW(
        p_dataset, v_snapshot, p_mode, p_namespace, p_series, p_months,
        p_cutoff, p_unit_name, p_metadata_version
    ) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'CONTEXT_IDENTITY_CONFLICT',
            SCHEMA = 'chronicle', TABLE = 'analysis_context', CONSTRAINT = 'api_context_identity_conflict';
    END IF;
    RETURN p_context_id;
END;
$$;

REVOKE ALL ON FUNCTION chronicle.create_context(
    uuid, text, text, text, text, text, date[], date, text, text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION chronicle.create_context(
    uuid, text, text, text, text, text, date[], date, text, text
) TO chronicle_reader;

-- Batch closure must count with a new statement snapshot after any lock wait.
-- Retain applied 001/002 bytes and replace only the protected entry point.
CREATE OR REPLACE FUNCTION chronicle.seal_batch(p_dataset_id text, p_batch_id text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE batch_state text; expected_count integer; actual_count bigint;
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SEAL_REQUIRES_READ_COMMITTED',
            SCHEMA = 'chronicle', TABLE = 'import_batch',
            CONSTRAINT = 'api_seal_requires_read_committed';
    END IF;
    SELECT state, expected_occurrence_count INTO batch_state, expected_count
        FROM chronicle.import_batch
        WHERE dataset_id = p_dataset_id AND batch_id = p_batch_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'BATCH_NOT_FOUND: %/%', p_dataset_id, p_batch_id
            USING ERRCODE = '23503', SCHEMA = 'chronicle', TABLE = 'import_batch',
            CONSTRAINT = 'api_batch_not_found';
    END IF;
    IF batch_state = 'validated' THEN
        RETURN;
    END IF;
    SELECT count(*) INTO actual_count FROM chronicle.revision_occurrence
        WHERE dataset_id = p_dataset_id AND batch_id = p_batch_id;
    IF actual_count <> expected_count THEN
        RAISE EXCEPTION 'BATCH_INCOMPLETE: expected %, found %', expected_count, actual_count
            USING ERRCODE = '23514', SCHEMA = 'chronicle', TABLE = 'import_batch',
            CONSTRAINT = 'api_batch_incomplete';
    END IF;
    UPDATE chronicle.import_batch SET state = 'validated'
        WHERE dataset_id = p_dataset_id AND batch_id = p_batch_id;
END;
$$;

REVOKE ALL ON FUNCTION chronicle.seal_batch(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION chronicle.seal_batch(text, text) TO chronicle_importer;
