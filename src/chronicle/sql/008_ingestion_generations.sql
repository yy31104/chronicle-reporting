-- Immutable ingestion identity, receipt binding and parser interpretation lineage.
CREATE TABLE chronicle.ingestion_request (
    request_id uuid PRIMARY KEY,
    dataset_id text NOT NULL REFERENCES chronicle.dataset,
    request_document jsonb NOT NULL CHECK(jsonb_typeof(request_document)='object'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE TABLE chronicle.interpretation_generation (
    dataset_id text PRIMARY KEY REFERENCES chronicle.dataset,
    first_request_id uuid NOT NULL UNIQUE REFERENCES chronicle.ingestion_request,
    parser_identity text NOT NULL CHECK(parser_identity ~ '^[0-9a-f]{64}$'),
    predecessor_dataset text,
    predecessor_snapshot text,
    defect_note text NOT NULL CHECK(length(defect_note) BETWEEN 1 AND 4000),
    interpreted_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    CHECK((predecessor_dataset IS NULL)=(predecessor_snapshot IS NULL)),
    CHECK(predecessor_dataset IS NULL OR predecessor_dataset<>dataset_id),
    FOREIGN KEY(predecessor_dataset,predecessor_snapshot) REFERENCES chronicle.snapshot
);
CREATE TABLE chronicle.ingestion_acquisition (
    request_id uuid NOT NULL REFERENCES chronicle.ingestion_request,
    source_id text NOT NULL CHECK(chronicle.valid_identifier(source_id)),
    ticket_id uuid NOT NULL REFERENCES chronicle.staging_ticket,
    worker_id uuid NOT NULL,
    object_sha256 text NOT NULL REFERENCES chronicle.storage_object,
    policy_sha256 text NOT NULL REFERENCES chronicle.rights_policy,
    source_document jsonb NOT NULL CHECK(jsonb_typeof(source_document)='object'),
    captured_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY(request_id,source_id)
);

CREATE FUNCTION chronicle.claim_ingestion(
    p_request uuid,p_dataset text,p_doc jsonb,p_parser text,
    p_predecessor_dataset text,p_predecessor_snapshot text,p_note text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE old_request chronicle.ingestion_request; generation chronicle.interpretation_generation;
BEGIN
    PERFORM 1 FROM chronicle.dataset WHERE dataset_id=p_dataset FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='UNKNOWN_DATASET',
        SCHEMA='chronicle',TABLE='ingestion_request',CONSTRAINT='ingest_unknown_dataset'; END IF;
    SELECT * INTO old_request FROM chronicle.ingestion_request WHERE request_id=p_request;
    IF FOUND THEN
        IF ROW(old_request.dataset_id,old_request.request_document)
            IS DISTINCT FROM ROW(p_dataset,p_doc) THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='INGESTION_IDENTITY_CONFLICT',
                SCHEMA='chronicle',TABLE='ingestion_request',CONSTRAINT='ingest_ingestion_identity_conflict';
        END IF;
        RETURN;
    END IF;
    SELECT * INTO generation FROM chronicle.interpretation_generation WHERE dataset_id=p_dataset;
    IF FOUND THEN
        IF generation.parser_identity IS DISTINCT FROM p_parser THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='PARSER_GENERATION_REQUIRED',
                SCHEMA='chronicle',TABLE='ingestion_request',CONSTRAINT='ingest_parser_generation_required';
        END IF;
        IF p_predecessor_dataset IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='GENERATION_ALREADY_EXISTS',
                SCHEMA='chronicle',TABLE='ingestion_request',CONSTRAINT='ingest_generation_already_exists';
        END IF;
    ELSE
        IF EXISTS(SELECT 1 FROM chronicle.import_batch WHERE dataset_id=p_dataset)
            OR EXISTS(SELECT 1 FROM chronicle.snapshot WHERE dataset_id=p_dataset) THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='GENERATION_NOT_EMPTY',
                SCHEMA='chronicle',TABLE='ingestion_request',CONSTRAINT='ingest_generation_not_empty';
        END IF;
    END IF;
    INSERT INTO chronicle.ingestion_request VALUES(p_request,p_dataset,p_doc,statement_timestamp());
    IF generation.dataset_id IS NULL THEN
        INSERT INTO chronicle.interpretation_generation(
            dataset_id,first_request_id,parser_identity,predecessor_dataset,
            predecessor_snapshot,defect_note
        ) VALUES(p_dataset,p_request,p_parser,p_predecessor_dataset,p_predecessor_snapshot,p_note);
    END IF;
END $$;

CREATE FUNCTION chronicle.record_ingestion_acquisition(
    p_request uuid,p_source text,p_ticket uuid,p_worker uuid,p_hash text,p_policy text,p_doc jsonb
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE old_row chronicle.ingestion_acquisition; request_dataset text;
BEGIN
    SELECT dataset_id INTO request_dataset FROM chronicle.ingestion_request
        WHERE request_id=p_request FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='INGESTION_NOT_FOUND',
        SCHEMA='chronicle',TABLE='ingestion_request',CONSTRAINT='ingest_ingestion_not_found'; END IF;
    IF NOT EXISTS(SELECT 1 FROM chronicle.staging_ticket t
        JOIN chronicle.ticket_object r USING(ticket_id)
        JOIN chronicle.storage_object o USING(object_sha256)
        WHERE t.ticket_id=p_ticket AND t.worker_id=p_worker AND t.dataset_id=request_dataset
          AND t.policy_sha256=p_policy AND t.state='active' AND r.object_sha256=p_hash
          AND o.state='ready' AND t.source_metadata=p_doc->'manifest') THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='INGESTION_OBJECT_MISMATCH',
            SCHEMA='chronicle',TABLE='ingestion_request',CONSTRAINT='ingest_ingestion_object_mismatch';
    END IF;
    INSERT INTO chronicle.ingestion_acquisition(
        request_id,source_id,ticket_id,worker_id,object_sha256,policy_sha256,source_document
    ) VALUES(p_request,p_source,p_ticket,p_worker,p_hash,p_policy,p_doc)
      ON CONFLICT(request_id,source_id) DO NOTHING;
    SELECT * INTO old_row FROM chronicle.ingestion_acquisition
        WHERE request_id=p_request AND source_id=p_source;
    IF ROW(old_row.ticket_id,old_row.worker_id,old_row.object_sha256,
           old_row.policy_sha256,old_row.source_document)
        IS DISTINCT FROM ROW(p_ticket,p_worker,p_hash,p_policy,p_doc) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='INGESTION_IDENTITY_CONFLICT',
            SCHEMA='chronicle',TABLE='ingestion_request',CONSTRAINT='ingest_ingestion_identity_conflict';
    END IF;
END $$;

DO $$ DECLARE n text; BEGIN
    FOREACH n IN ARRAY ARRAY['ingestion_request','interpretation_generation','ingestion_acquisition']
    LOOP
        EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON chronicle.%I '
            'FOR EACH ROW EXECUTE FUNCTION chronicle.reject_artifact_mutation()',n);
        EXECUTE format('CREATE TRIGGER immutable_table BEFORE TRUNCATE ON chronicle.%I '
            'FOR EACH STATEMENT EXECUTE FUNCTION chronicle.reject_artifact_mutation()',n);
    END LOOP;
END $$;
REVOKE ALL ON FUNCTION chronicle.claim_ingestion(uuid,text,jsonb,text,text,text,text),
    chronicle.record_ingestion_acquisition(uuid,text,uuid,uuid,text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION chronicle.claim_ingestion(uuid,text,jsonb,text,text,text,text),
    chronicle.record_ingestion_acquisition(uuid,text,uuid,uuid,text,text,jsonb) TO chronicle_publisher;
GRANT SELECT ON chronicle.ingestion_request,chronicle.interpretation_generation,
    chronicle.ingestion_acquisition TO chronicle_publisher,chronicle_reader;
