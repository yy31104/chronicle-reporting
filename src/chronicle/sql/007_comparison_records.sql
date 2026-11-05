-- Four controlled server-owned executions and their canonical decomposition.
CREATE TABLE chronicle.comparison_run (
    comparison_id uuid PRIMARY KEY,
    old_context_id uuid NOT NULL REFERENCES chronicle.analysis_context(context_id),
    new_context_id uuid NOT NULL REFERENCES chronicle.analysis_context(context_id),
    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    document_sha256 text NOT NULL CHECK (document_sha256 ~ '^[0-9a-f]{64}$'),
    document jsonb NOT NULL CHECK (
        jsonb_typeof(document) = 'object'
        AND document ? 'comparison_id'
        AND jsonb_typeof(document->'comparison_id') = 'string'
        AND document->>'comparison_id' = comparison_id::text
        AND octet_length(document::text) <= 67108864
    ),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON chronicle.comparison_run
    FOR EACH ROW EXECUTE FUNCTION chronicle.reject_report_mutation();
CREATE TRIGGER immutable_table BEFORE TRUNCATE ON chronicle.comparison_run
    FOR EACH STATEMENT EXECUTE FUNCTION chronicle.reject_report_mutation();
REVOKE ALL ON chronicle.comparison_run FROM PUBLIC;
GRANT SELECT ON chronicle.comparison_run TO chronicle_reader;
GRANT INSERT (comparison_id,old_context_id,new_context_id,request_sha256,
    document_sha256,document) ON chronicle.comparison_run TO chronicle_reader;
