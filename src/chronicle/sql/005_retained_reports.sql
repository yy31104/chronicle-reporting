-- Append-only, atomic retained reports. Python is the trusted formula executor;
-- the database protects identity, context references, bounds and immutability.
CREATE TABLE chronicle.report_run (
    report_id uuid PRIMARY KEY,
    context_id uuid NOT NULL REFERENCES chronicle.analysis_context(context_id),
    input_context_id uuid NOT NULL REFERENCES chronicle.analysis_context(context_id),
    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    implementation_sha256 text NOT NULL CHECK (implementation_sha256 ~ '^[0-9a-f]{64}$'),
    dependency_lock_sha256 text NOT NULL CHECK (dependency_lock_sha256 ~ '^[0-9a-f]{64}$'),
    document_sha256 text NOT NULL CHECK (document_sha256 ~ '^[0-9a-f]{64}$'),
    document jsonb NOT NULL CHECK (
        jsonb_typeof(document) = 'object'
        AND document ?& ARRAY['report_id', 'context_id', 'input_context_id']
        AND jsonb_typeof(document->'report_id') = 'string'
        AND jsonb_typeof(document->'context_id') = 'string'
        AND jsonb_typeof(document->'input_context_id') = 'string'
        AND document->>'report_id' = report_id::text
        AND document->>'context_id' = context_id::text
        AND document->>'input_context_id' = input_context_id::text
        AND octet_length(document::text) <= 16777216
    ),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp()
);

CREATE FUNCTION chronicle.reject_report_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'IMMUTABLE_REPORT';
END;
$$;

CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON chronicle.report_run
    FOR EACH ROW EXECUTE FUNCTION chronicle.reject_report_mutation();
CREATE TRIGGER immutable_table BEFORE TRUNCATE ON chronicle.report_run
    FOR EACH STATEMENT EXECUTE FUNCTION chronicle.reject_report_mutation();

REVOKE ALL ON chronicle.report_run FROM PUBLIC;
REVOKE ALL ON FUNCTION chronicle.reject_report_mutation() FROM PUBLIC;
GRANT SELECT ON chronicle.report_run TO chronicle_reader;
GRANT INSERT (report_id,context_id,input_context_id,request_sha256,
    implementation_sha256,dependency_lock_sha256,document_sha256,document)
    ON chronicle.report_run TO chronicle_reader;
