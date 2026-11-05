-- Batch closure must count with a new statement snapshot after any lock wait.
-- Retain applied 001/002 bytes and replace only the protected entry point.
CREATE OR REPLACE FUNCTION chronicle.seal_batch(p_dataset_id text, p_batch_id text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE batch_state text; expected_count integer; actual_count bigint;
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'SEAL_REQUIRES_READ_COMMITTED';
    END IF;
    SELECT state, expected_occurrence_count INTO batch_state, expected_count
        FROM chronicle.import_batch
        WHERE dataset_id = p_dataset_id AND batch_id = p_batch_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'BATCH_NOT_FOUND: %/%', p_dataset_id, p_batch_id
            USING ERRCODE = '23503';
    END IF;
    IF batch_state = 'validated' THEN
        RETURN;
    END IF;
    SELECT count(*) INTO actual_count FROM chronicle.revision_occurrence
        WHERE dataset_id = p_dataset_id AND batch_id = p_batch_id;
    IF actual_count <> expected_count THEN
        RAISE EXCEPTION 'BATCH_INCOMPLETE: expected %, found %', expected_count, actual_count
            USING ERRCODE = '23514';
    END IF;
    UPDATE chronicle.import_batch SET state = 'validated'
        WHERE dataset_id = p_dataset_id AND batch_id = p_batch_id;
END;
$$;

REVOKE ALL ON FUNCTION chronicle.seal_batch(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION chronicle.seal_batch(text, text) TO chronicle_importer;
