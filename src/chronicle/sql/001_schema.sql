-- C1-PR02: normalized synthetic inputs and immutable snapshot/context storage.
-- Run under SET ROLE chronicle_owner after provisioning the four local roles.

CREATE SCHEMA IF NOT EXISTS chronicle AUTHORIZATION chronicle_owner;
REVOKE ALL ON SCHEMA chronicle FROM PUBLIC;
GRANT USAGE ON SCHEMA chronicle
    TO chronicle_importer, chronicle_publisher, chronicle_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA chronicle REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE FUNCTION chronicle.valid_identifier(value text) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT value <> '' AND char_length(value) <= 200
       AND value = btrim(value,
           U&' \0009\000A\000B\000C\000D\001C\001D\001E\001F\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000');
$$;

CREATE FUNCTION chronicle.valid_month(value date) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT CASE WHEN value BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'
        THEN extract(day FROM value) = 1 ELSE false END;
$$;

CREATE FUNCTION chronicle.valid_months(value date[]) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT array_ndims(value) = 1 AND array_lower(value, 1) = 1
       AND cardinality(value) > 0
       AND NOT EXISTS (
           SELECT 1 FROM unnest(value) AS item(month)
           WHERE month IS NULL OR NOT chronicle.valid_month(month)
       )
       AND (SELECT count(*) = count(DISTINCT month) FROM unnest(value) AS item(month));
$$;

CREATE FUNCTION chronicle.valid_decimal(value text) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
    SELECT CASE WHEN char_length(value) <= 128
        AND value COLLATE "C" ~ '^[+-]?[0-9]+([.][0-9]{1,12})?$'
        THEN abs(value::numeric) <= 99999999999999999999999999.999999999999
        ELSE false END;
$$;

CREATE TABLE chronicle.dataset (
    dataset_id text PRIMARY KEY CHECK (chronicle.valid_identifier(dataset_id)),
    current_snapshot_id text
);

CREATE TABLE chronicle.source_namespace (
    dataset_id text NOT NULL REFERENCES chronicle.dataset,
    source_namespace text NOT NULL CHECK (chronicle.valid_identifier(source_namespace)),
    order_rule text NOT NULL CHECK (order_rule IN ('date_ordinal', 'documented_sequence')),
    PRIMARY KEY (dataset_id, source_namespace),
    UNIQUE (dataset_id, source_namespace, order_rule)
);

CREATE TABLE chronicle.series (
    dataset_id text NOT NULL,
    source_namespace text NOT NULL,
    series_key text NOT NULL CHECK (chronicle.valid_identifier(series_key)),
    PRIMARY KEY (dataset_id, source_namespace, series_key),
    FOREIGN KEY (dataset_id, source_namespace)
        REFERENCES chronicle.source_namespace
);

CREATE TABLE chronicle.source_release (
    dataset_id text NOT NULL,
    source_namespace text NOT NULL,
    source_revision_key text NOT NULL CHECK (chronicle.valid_identifier(source_revision_key)),
    source_order bigint NOT NULL,
    order_rule text NOT NULL,
    availability_precision text NOT NULL CHECK (availability_precision IN ('date', 'unknown')),
    release_day date,
    PRIMARY KEY (dataset_id, source_namespace, source_revision_key),
    UNIQUE (dataset_id, source_namespace, source_order),
    FOREIGN KEY (dataset_id, source_namespace, order_rule)
        REFERENCES chronicle.source_namespace (dataset_id, source_namespace, order_rule),
    CHECK (
        (availability_precision = 'date' AND release_day IS NOT NULL
            AND release_day BETWEEN DATE '0001-01-01' AND DATE '9999-12-31')
        OR (availability_precision = 'unknown' AND release_day IS NULL)
    ),
    CHECK (
        order_rule <> 'date_ordinal'
        OR (availability_precision = 'date'
            AND source_order = release_day - DATE '0001-01-01' + 1)
    )
);

CREATE TABLE chronicle.revision (
    dataset_id text NOT NULL,
    source_namespace text NOT NULL,
    series_key text NOT NULL,
    observation_month date NOT NULL CHECK (chronicle.valid_month(observation_month)),
    source_revision_key text NOT NULL,
    status text NOT NULL CHECK (status IN ('value', 'missing', 'withdrawn')),
    value text,
    unit_name text NOT NULL CHECK (chronicle.valid_identifier(unit_name)),
    metadata_version text NOT NULL CHECK (chronicle.valid_identifier(metadata_version)),
    PRIMARY KEY (
        dataset_id, source_namespace, series_key, observation_month, source_revision_key
    ),
    FOREIGN KEY (dataset_id, source_namespace, series_key) REFERENCES chronicle.series,
    FOREIGN KEY (dataset_id, source_namespace, source_revision_key)
        REFERENCES chronicle.source_release,
    CHECK (
        (status = 'value' AND value IS NOT NULL AND chronicle.valid_decimal(value))
        OR (status IN ('missing', 'withdrawn') AND value IS NULL)
    )
);

CREATE TABLE chronicle.import_batch (
    dataset_id text NOT NULL REFERENCES chronicle.dataset,
    batch_id text NOT NULL CHECK (chronicle.valid_identifier(batch_id)),
    state text NOT NULL DEFAULT 'staging' CHECK (state IN ('staging', 'validated')),
    content_sha256 text NOT NULL CHECK (content_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'),
    expected_occurrence_count integer NOT NULL CHECK (expected_occurrence_count >= 0),
    PRIMARY KEY (dataset_id, batch_id)
);

CREATE TABLE chronicle.revision_occurrence (
    dataset_id text NOT NULL,
    batch_id text NOT NULL,
    source_namespace text NOT NULL,
    series_key text NOT NULL,
    observation_month date NOT NULL,
    source_revision_key text NOT NULL,
    received_at timestamptz NOT NULL CHECK (
        received_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'
        AND received_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'
    ),
    PRIMARY KEY (
        dataset_id, batch_id, source_namespace, series_key, observation_month,
        source_revision_key, received_at
    ),
    FOREIGN KEY (dataset_id, batch_id) REFERENCES chronicle.import_batch,
    FOREIGN KEY (
        dataset_id, source_namespace, series_key, observation_month, source_revision_key
    ) REFERENCES chronicle.revision
);

CREATE TABLE chronicle.snapshot (
    dataset_id text NOT NULL REFERENCES chronicle.dataset,
    snapshot_id text NOT NULL CHECK (chronicle.valid_identifier(snapshot_id)),
    fixture_manifest_sha256 text NOT NULL
        CHECK (fixture_manifest_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (dataset_id, snapshot_id)
);

ALTER TABLE chronicle.dataset ADD CONSTRAINT dataset_current_snapshot_fk
    FOREIGN KEY (dataset_id, current_snapshot_id)
    REFERENCES chronicle.snapshot (dataset_id, snapshot_id);

CREATE TABLE chronicle.snapshot_batch (
    dataset_id text NOT NULL,
    snapshot_id text NOT NULL,
    batch_id text NOT NULL,
    PRIMARY KEY (dataset_id, snapshot_id, batch_id),
    FOREIGN KEY (dataset_id, snapshot_id) REFERENCES chronicle.snapshot,
    FOREIGN KEY (dataset_id, batch_id) REFERENCES chronicle.import_batch
);

CREATE TABLE chronicle.analysis_context (
    context_id uuid PRIMARY KEY,
    dataset_id text NOT NULL,
    snapshot_id text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('latest_corrected', 'source_as_of', 'system_snapshot')),
    source_namespace text NOT NULL CHECK (chronicle.valid_identifier(source_namespace)),
    series_key text NOT NULL CHECK (chronicle.valid_identifier(series_key)),
    months date[] NOT NULL CHECK (chronicle.valid_months(months)),
    exclusive_end_date date,
    expected_unit_name text CHECK (chronicle.valid_identifier(expected_unit_name)),
    expected_metadata_version text CHECK (chronicle.valid_identifier(expected_metadata_version)),
    FOREIGN KEY (dataset_id, snapshot_id) REFERENCES chronicle.snapshot,
    CHECK (
        (mode = 'source_as_of' AND exclusive_end_date IS NOT NULL
            AND exclusive_end_date BETWEEN DATE '0001-01-01' AND DATE '9999-12-31')
        OR (mode <> 'source_as_of' AND exclusive_end_date IS NULL)
    ),
    CHECK ((expected_unit_name IS NULL) = (expected_metadata_version IS NULL))
);

CREATE FUNCTION chronicle.reject_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    -- Only owner-executed narrow transition functions may make these updates.
    IF TG_OP = 'UPDATE' AND current_user = 'chronicle_owner' THEN
        IF TG_TABLE_NAME = 'dataset' AND NEW.dataset_id = OLD.dataset_id THEN
            RETURN NEW;
        ELSIF TG_TABLE_NAME = 'import_batch' THEN
            IF NEW.dataset_id = OLD.dataset_id AND NEW.batch_id = OLD.batch_id
                AND NEW.content_sha256 = OLD.content_sha256
                AND NEW.expected_occurrence_count = OLD.expected_occurrence_count
                AND OLD.state = 'staging' AND NEW.state = 'validated' THEN
                RETURN NEW;
            END IF;
        END IF;
    END IF;
    RAISE EXCEPTION 'IMMUTABLE_RECORD: %.% rejects %', TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
        USING ERRCODE = '55000';
END;
$$;

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'dataset', 'source_namespace', 'series', 'source_release', 'revision',
        'import_batch', 'revision_occurrence', 'snapshot', 'snapshot_batch', 'analysis_context'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON chronicle.%I '
            'FOR EACH ROW EXECUTE FUNCTION chronicle.reject_mutation()', table_name
        );
        EXECUTE format(
            'CREATE TRIGGER immutable_table BEFORE TRUNCATE ON chronicle.%I '
            'FOR EACH STATEMENT EXECUTE FUNCTION chronicle.reject_mutation()', table_name
        );
    END LOOP;
END;
$$;

CREATE FUNCTION chronicle.guard_batch_occurrence() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE batch_state text;
BEGIN
    SELECT state INTO batch_state FROM chronicle.import_batch
        WHERE dataset_id = NEW.dataset_id AND batch_id = NEW.batch_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'BATCH_NOT_FOUND: %/%', NEW.dataset_id, NEW.batch_id
            USING ERRCODE = '23503';
    END IF;
    IF batch_state <> 'staging' THEN
        RAISE EXCEPTION 'BATCH_SEALED: %/%', NEW.dataset_id, NEW.batch_id
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER occurrence_requires_open_batch BEFORE INSERT ON chronicle.revision_occurrence
    FOR EACH ROW EXECUTE FUNCTION chronicle.guard_batch_occurrence();

CREATE FUNCTION chronicle.seal_batch(p_dataset_id text, p_batch_id text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE batch_state text; expected_count integer; actual_count bigint;
BEGIN
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

REVOKE ALL ON ALL TABLES IN SCHEMA chronicle FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA chronicle FROM PUBLIC;
GRANT SELECT ON ALL TABLES IN SCHEMA chronicle TO chronicle_reader;
GRANT SELECT ON chronicle.dataset, chronicle.source_namespace, chronicle.series,
    chronicle.source_release, chronicle.revision, chronicle.import_batch,
    chronicle.revision_occurrence TO chronicle_importer;
GRANT INSERT (dataset_id) ON chronicle.dataset TO chronicle_importer;
GRANT INSERT (dataset_id, batch_id, content_sha256, expected_occurrence_count)
    ON chronicle.import_batch TO chronicle_importer;
GRANT INSERT ON chronicle.source_namespace, chronicle.series, chronicle.source_release,
    chronicle.revision, chronicle.revision_occurrence TO chronicle_importer;
GRANT EXECUTE ON FUNCTION chronicle.valid_identifier(text), chronicle.valid_month(date),
    chronicle.valid_decimal(text) TO chronicle_importer;
GRANT EXECUTE ON FUNCTION chronicle.seal_batch(text, text) TO chronicle_importer;
