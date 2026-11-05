-- Service-owned, content-addressed artifact publication. Applied bytes are immutable.
CREATE TABLE chronicle.managed_dataset (
    dataset_id text PRIMARY KEY REFERENCES chronicle.dataset
);
CREATE TABLE chronicle.rights_policy (
    policy_sha256 text PRIMARY KEY CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
    policy jsonb NOT NULL CHECK (jsonb_typeof(policy) = 'object'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE TABLE chronicle.rights_revocation (
    policy_sha256 text PRIMARY KEY REFERENCES chronicle.rights_policy,
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 1000),
    revoked_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE TABLE chronicle.storage_object (
    object_sha256 text PRIMARY KEY CHECK (object_sha256 ~ '^[0-9a-f]{64}$'),
    byte_length bigint NOT NULL CHECK (byte_length BETWEEN 0 AND 33554432),
    state text NOT NULL CHECK (state IN ('staged','ready','deleting','deleted')),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
CREATE TABLE chronicle.staging_ticket (
    ticket_id uuid PRIMARY KEY,
    dataset_id text NOT NULL REFERENCES chronicle.dataset,
    worker_id uuid NOT NULL,
    policy_sha256 text NOT NULL REFERENCES chronicle.rights_policy,
    source_metadata jsonb NOT NULL CHECK (jsonb_typeof(source_metadata) = 'object'),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','closed','abandoned')),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    ended_at timestamptz,
    CHECK ((state = 'active') = (ended_at IS NULL))
);
CREATE TABLE chronicle.ticket_object (
    ticket_id uuid NOT NULL REFERENCES chronicle.staging_ticket,
    object_sha256 text NOT NULL REFERENCES chronicle.storage_object,
    PRIMARY KEY (ticket_id,object_sha256)
);
CREATE TABLE chronicle.batch_artifact (
    dataset_id text NOT NULL,
    batch_id text NOT NULL,
    object_sha256 text NOT NULL REFERENCES chronicle.storage_object,
    policy_sha256 text NOT NULL REFERENCES chronicle.rights_policy,
    source_metadata jsonb NOT NULL CHECK (jsonb_typeof(source_metadata) = 'object'),
    PRIMARY KEY (dataset_id,batch_id,object_sha256),
    FOREIGN KEY (dataset_id,batch_id) REFERENCES chronicle.import_batch
);
CREATE TABLE chronicle.artifact_publication (
    request_id uuid PRIMARY KEY,
    dataset_id text NOT NULL,
    snapshot_id text NOT NULL,
    expected_parent text,
    request_document jsonb NOT NULL,
    manifest_sha256 text NOT NULL REFERENCES chronicle.storage_object,
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    UNIQUE(dataset_id,snapshot_id),
    FOREIGN KEY(dataset_id,snapshot_id) REFERENCES chronicle.snapshot
);
CREATE TABLE chronicle.snapshot_object (
    dataset_id text NOT NULL,
    snapshot_id text NOT NULL,
    object_sha256 text NOT NULL REFERENCES chronicle.storage_object,
    PRIMARY KEY(dataset_id,snapshot_id,object_sha256),
    FOREIGN KEY(dataset_id,snapshot_id) REFERENCES chronicle.snapshot
);
CREATE INDEX snapshot_object_by_hash ON chronicle.snapshot_object(object_sha256);
CREATE INDEX ticket_object_by_hash ON chronicle.ticket_object(object_sha256);
CREATE TABLE chronicle.reader_pin (
    pin_id uuid PRIMARY KEY,
    dataset_id text NOT NULL,
    snapshot_id text NOT NULL,
    worker_id uuid NOT NULL,
    acquired_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    released_at timestamptz,
    FOREIGN KEY(dataset_id,snapshot_id) REFERENCES chronicle.snapshot
);
CREATE INDEX reader_pin_active ON chronicle.reader_pin(dataset_id,snapshot_id)
    WHERE released_at IS NULL;

-- Deterministic advisory lock namespace shared by registration, promotion,
-- publication and collection. Hash collisions serialize unrelated objects.
CREATE FUNCTION chronicle.object_lock_key(p_hash text) RETURNS bigint
LANGUAGE sql IMMUTABLE STRICT SET search_path=pg_catalog AS $$
    SELECT ('x' || substr(p_hash,1,16))::bit(64)::bigint
$$;

CREATE FUNCTION chronicle.open_object_ticket(
    p_ticket uuid,p_dataset text,p_worker uuid,p_policy_text text,p_metadata jsonb
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE policy_hash text; policy_doc jsonb; old_ticket chronicle.staging_ticket;
BEGIN
    PERFORM 1 FROM chronicle.dataset WHERE dataset_id=p_dataset FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='DATASET_NOT_FOUND',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_dataset_not_found';
    END IF;
    policy_doc := p_policy_text::jsonb;
    IF jsonb_typeof(policy_doc) <> 'object'
       OR policy_doc->>'decision_state' IS DISTINCT FROM 'approved_scoped'
       OR NOT COALESCE(policy_doc->'permitted_uses' ? 'local_ingest',FALSE) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='RIGHTS_DENIED',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_rights_denied';
    END IF;
    policy_hash := encode(sha256(convert_to(p_policy_text,'UTF8')),'hex');
    INSERT INTO chronicle.rights_policy(policy_sha256,policy) VALUES(policy_hash,policy_doc)
        ON CONFLICT(policy_sha256) DO NOTHING;
    IF EXISTS(SELECT 1 FROM chronicle.rights_revocation WHERE policy_sha256=policy_hash) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='RIGHTS_DENIED',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_rights_denied';
    END IF;
    INSERT INTO chronicle.managed_dataset VALUES(p_dataset) ON CONFLICT DO NOTHING;
    INSERT INTO chronicle.staging_ticket(ticket_id,dataset_id,worker_id,policy_sha256,source_metadata)
        VALUES(p_ticket,p_dataset,p_worker,policy_hash,p_metadata)
        ON CONFLICT(ticket_id) DO NOTHING;
    SELECT * INTO old_ticket FROM chronicle.staging_ticket WHERE ticket_id=p_ticket FOR UPDATE;
    IF ROW(old_ticket.dataset_id,old_ticket.worker_id,old_ticket.policy_sha256,
           old_ticket.source_metadata,old_ticket.state)
       IS DISTINCT FROM ROW(p_dataset,p_worker,policy_hash,p_metadata,'active'::text) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='TICKET_IDENTITY_CONFLICT',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_ticket_identity_conflict';
    END IF;
    RETURN policy_hash;
END $$;

CREATE FUNCTION chronicle.register_object(
    p_ticket uuid,p_worker uuid,p_hash text,p_length bigint
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE obj chronicle.storage_object;
BEGIN
    PERFORM pg_advisory_xact_lock(chronicle.object_lock_key(p_hash));
    IF NOT EXISTS(SELECT 1 FROM chronicle.staging_ticket
                  WHERE ticket_id=p_ticket AND worker_id=p_worker AND state='active' FOR UPDATE) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='TICKET_NOT_ACTIVE',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_ticket_not_active';
    END IF;
    INSERT INTO chronicle.storage_object(object_sha256,byte_length,state)
        VALUES(p_hash,p_length,'staged') ON CONFLICT(object_sha256) DO NOTHING;
    SELECT * INTO obj FROM chronicle.storage_object WHERE object_sha256=p_hash FOR UPDATE;
    IF obj.byte_length <> p_length OR obj.state = 'deleting' THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='OBJECT_IDENTITY_CONFLICT',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_object_identity_conflict';
    END IF;
    UPDATE chronicle.storage_object SET state='staged'
        WHERE object_sha256=p_hash AND state='deleted';
    INSERT INTO chronicle.ticket_object VALUES(p_ticket,p_hash) ON CONFLICT DO NOTHING;
END $$;

CREATE FUNCTION chronicle.object_ready(p_ticket uuid,p_worker uuid,p_hash text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(chronicle.object_lock_key(p_hash));
    IF NOT EXISTS(SELECT 1 FROM chronicle.staging_ticket t JOIN chronicle.ticket_object r
                  ON r.ticket_id=t.ticket_id WHERE t.ticket_id=p_ticket AND t.worker_id=p_worker
                  AND t.state='active' AND r.object_sha256=p_hash FOR UPDATE OF t) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='TICKET_NOT_ACTIVE',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_ticket_not_active';
    END IF;
    UPDATE chronicle.storage_object SET state='ready'
        WHERE object_sha256=p_hash AND state IN ('staged','ready');
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='OBJECT_NOT_READY',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_object_not_ready';
    END IF;
END $$;

CREATE FUNCTION chronicle.end_object_ticket(p_ticket uuid,p_worker uuid,p_abandoned boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    UPDATE chronicle.staging_ticket SET state=CASE WHEN p_abandoned THEN 'abandoned' ELSE 'closed' END,
        ended_at=statement_timestamp()
        WHERE ticket_id=p_ticket AND worker_id=p_worker AND state='active';
    IF NOT FOUND AND NOT EXISTS(SELECT 1 FROM chronicle.staging_ticket WHERE ticket_id=p_ticket
                               AND worker_id=p_worker AND state IN ('closed','abandoned')) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='TICKET_NOT_OWNED',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_ticket_not_owned';
    END IF;
END $$;

CREATE FUNCTION chronicle.attach_batch_artifact(
    p_dataset text,p_batch text,p_ticket uuid,p_worker uuid,p_hash text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE ticket chronicle.staging_ticket; old_artifact chronicle.batch_artifact;
BEGIN
    -- Dataset-before-object ordering matches publication; attaching evidence to
    -- an already published batch would rewrite its promised object closure.
    PERFORM 1 FROM chronicle.dataset WHERE dataset_id=p_dataset FOR UPDATE;
    PERFORM pg_advisory_xact_lock(chronicle.object_lock_key(p_hash));
    SELECT * INTO ticket FROM chronicle.staging_ticket WHERE ticket_id=p_ticket FOR UPDATE;
    IF NOT FOUND OR ROW(ticket.dataset_id,ticket.worker_id,ticket.state)
        IS DISTINCT FROM ROW(p_dataset,p_worker,'active'::text) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='TICKET_NOT_ACTIVE',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_ticket_not_active';
    END IF;
    SELECT * INTO old_artifact FROM chronicle.batch_artifact
        WHERE dataset_id=p_dataset AND batch_id=p_batch AND object_sha256=p_hash;
    IF FOUND THEN
        IF ROW(old_artifact.policy_sha256,old_artifact.source_metadata)
            IS DISTINCT FROM ROW(ticket.policy_sha256,ticket.source_metadata) THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='ARTIFACT_IDENTITY_CONFLICT',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_artifact_identity_conflict';
        END IF;
        RETURN;
    END IF;
    IF EXISTS(SELECT 1 FROM chronicle.snapshot_batch WHERE dataset_id=p_dataset AND batch_id=p_batch)
       OR NOT EXISTS(SELECT 1 FROM chronicle.import_batch WHERE dataset_id=p_dataset
                     AND batch_id=p_batch AND state='validated') THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='BATCH_ARTIFACT_CLOSED',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_batch_artifact_closed';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM chronicle.ticket_object t JOIN chronicle.storage_object o
                  USING(object_sha256) WHERE t.ticket_id=p_ticket AND t.object_sha256=p_hash
                  AND o.state='ready')
       OR EXISTS(SELECT 1 FROM chronicle.rights_revocation WHERE policy_sha256=ticket.policy_sha256)
    THEN RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='OBJECT_NOT_READY',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_object_not_ready'; END IF;
    INSERT INTO chronicle.batch_artifact VALUES(
        p_dataset,p_batch,p_hash,ticket.policy_sha256,ticket.source_metadata);
END $$;

CREATE FUNCTION chronicle.artifact_manifest(
    p_dataset text,p_snapshot text,p_parent text,p_batches text[]
) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
    SELECT jsonb_build_object(
        'schema','chronicle.snapshot-artifacts.v1','dataset_id',p_dataset,
        'snapshot_id',p_snapshot,'expected_parent',p_parent,
        'batch_ids',to_jsonb(p_batches),
        'artifacts',COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'batch_id',a.batch_id,'object_sha256',a.object_sha256,
            'byte_length',o.byte_length,'policy_sha256',a.policy_sha256,
            'source_metadata',a.source_metadata
        ) ORDER BY a.batch_id COLLATE "C",a.object_sha256 COLLATE "C")
        FROM chronicle.batch_artifact a JOIN chronicle.storage_object o USING(object_sha256)
        WHERE a.dataset_id=p_dataset AND a.batch_id=ANY(p_batches)), '[]'::jsonb))
$$;

CREATE FUNCTION chronicle.publish_artifact_snapshot(
    p_request uuid,p_dataset text,p_snapshot text,p_parent text,p_new_batches text[],
    p_manifest_text text,p_ticket uuid,p_worker uuid,p_manifest_hash text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE old_request chronicle.artifact_publication; request_doc jsonb; batches text[];
    pointer text; object_hash text; manifest jsonb;
BEGIN
    request_doc := jsonb_build_object('dataset_id',p_dataset,'snapshot_id',p_snapshot,
        'expected_parent',p_parent,'new_batch_ids',to_jsonb(p_new_batches));
    PERFORM 1 FROM chronicle.dataset WHERE dataset_id=p_dataset FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='DATASET_NOT_FOUND',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_dataset_not_found'; END IF;
    SELECT * INTO old_request FROM chronicle.artifact_publication WHERE request_id=p_request;
    IF FOUND THEN
        IF old_request.request_document IS DISTINCT FROM request_doc THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='PUBLICATION_IDENTITY_CONFLICT',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_publication_identity_conflict';
        END IF;
        RETURN old_request.snapshot_id;
    END IF;
    SELECT current_snapshot_id INTO pointer FROM chronicle.dataset WHERE dataset_id=p_dataset;
    IF pointer IS DISTINCT FROM p_parent THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='SNAPSHOT_BASE_CHANGED',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_snapshot_base_changed';
    END IF;
    IF EXISTS(SELECT 1 FROM chronicle.snapshot WHERE dataset_id=p_dataset AND snapshot_id=p_snapshot)
       OR cardinality(p_new_batches) IS NULL OR cardinality(p_new_batches)=0
       OR EXISTS(SELECT 1 FROM unnest(p_new_batches) b WHERE b IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='INVALID_PUBLICATION',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_invalid_publication';
    END IF;
    SELECT array_agg(batch_id ORDER BY batch_id COLLATE "C") INTO batches FROM (
        SELECT batch_id FROM chronicle.snapshot_batch
            WHERE dataset_id=p_dataset AND snapshot_id=p_parent
        UNION SELECT unnest(p_new_batches)
    ) b;
    IF EXISTS(SELECT 1 FROM unnest(batches) b WHERE NOT EXISTS(
        SELECT 1 FROM chronicle.import_batch x WHERE x.dataset_id=p_dataset
            AND x.batch_id=b AND x.state='validated') OR NOT EXISTS(
        SELECT 1 FROM chronicle.batch_artifact a WHERE a.dataset_id=p_dataset AND a.batch_id=b)) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='ARTIFACT_CLOSURE_INCOMPLETE',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_artifact_closure_incomplete';
    END IF;
    manifest := chronicle.artifact_manifest(p_dataset,p_snapshot,p_parent,batches);
    IF manifest IS DISTINCT FROM p_manifest_text::jsonb OR p_manifest_hash IS DISTINCT FROM
        encode(sha256(convert_to(p_manifest_text,'UTF8')),'hex') THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='MANIFEST_MISMATCH',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_manifest_mismatch';
    END IF;
    FOR object_hash IN SELECT object_sha256 FROM (
        SELECT object_sha256 FROM chronicle.batch_artifact
            WHERE dataset_id=p_dataset AND batch_id=ANY(batches)
        UNION SELECT p_manifest_hash
    ) hashes ORDER BY object_sha256 COLLATE "C" LOOP
        PERFORM pg_advisory_xact_lock(chronicle.object_lock_key(object_hash));
        IF NOT EXISTS(SELECT 1 FROM chronicle.storage_object WHERE object_sha256=object_hash
                      AND state='ready' FOR UPDATE) THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='OBJECT_NOT_READY',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_object_not_ready';
        END IF;
    END LOOP;
    -- Lock policy rows so a revocation and publication have a linear order.
    PERFORM 1 FROM chronicle.rights_policy p WHERE p.policy_sha256 IN (
        SELECT policy_sha256 FROM chronicle.batch_artifact WHERE dataset_id=p_dataset
            AND batch_id=ANY(batches)) ORDER BY p.policy_sha256 FOR SHARE;
    IF EXISTS(SELECT 1 FROM chronicle.batch_artifact a JOIN chronicle.rights_revocation r
              USING(policy_sha256) WHERE a.dataset_id=p_dataset AND a.batch_id=ANY(batches)) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='RIGHTS_DENIED',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_rights_denied';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM chronicle.staging_ticket t JOIN chronicle.ticket_object o
        USING(ticket_id) WHERE t.ticket_id=p_ticket AND t.worker_id=p_worker AND t.dataset_id=p_dataset
        AND t.state='active' AND o.object_sha256=p_manifest_hash FOR UPDATE OF t) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='TICKET_NOT_ACTIVE',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_ticket_not_active';
    END IF;
    PERFORM chronicle.publish_snapshot_legacy(p_dataset,p_snapshot,batches,p_manifest_hash,p_parent);
    INSERT INTO chronicle.artifact_publication VALUES(
        p_request,p_dataset,p_snapshot,p_parent,request_doc,p_manifest_hash,statement_timestamp());
    INSERT INTO chronicle.snapshot_object
        SELECT p_dataset,p_snapshot,object_sha256 FROM (
            SELECT object_sha256 FROM chronicle.batch_artifact
                WHERE dataset_id=p_dataset AND batch_id=ANY(batches)
            UNION SELECT p_manifest_hash
        ) refs;
    PERFORM chronicle.end_object_ticket(p_ticket,p_worker,FALSE);
    RETURN p_snapshot;
END $$;

CREATE FUNCTION chronicle.revoke_rights(p_hash text,p_reason text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    PERFORM 1 FROM chronicle.rights_policy WHERE policy_sha256=p_hash FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='POLICY_NOT_FOUND',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_policy_not_found'; END IF;
    INSERT INTO chronicle.rights_revocation(policy_sha256,reason) VALUES(p_hash,p_reason)
        ON CONFLICT(policy_sha256) DO NOTHING;
END $$;

CREATE FUNCTION chronicle.begin_object_deletion(p_hash text,p_grace_seconds integer)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    IF p_grace_seconds IS NULL OR p_grace_seconds < 0 THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='INVALID_GRACE_PERIOD',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_invalid_grace_period';
    END IF;
    PERFORM pg_advisory_xact_lock(chronicle.object_lock_key(p_hash));
    PERFORM 1 FROM chronicle.storage_object WHERE object_sha256=p_hash FOR UPDATE;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    IF EXISTS(SELECT 1 FROM chronicle.snapshot_object WHERE object_sha256=p_hash)
       OR EXISTS(SELECT 1 FROM chronicle.ticket_object o JOIN chronicle.staging_ticket t USING(ticket_id)
            WHERE o.object_sha256=p_hash AND (t.state='active' OR t.ended_at >
                statement_timestamp() - make_interval(secs=>p_grace_seconds))) THEN
        RETURN FALSE;
    END IF;
    UPDATE chronicle.storage_object SET state='deleting'
        WHERE object_sha256=p_hash AND state IN ('staged','ready','deleting');
    RETURN FOUND;
END $$;

CREATE FUNCTION chronicle.finish_object_deletion(p_hash text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(chronicle.object_lock_key(p_hash));
    UPDATE chronicle.storage_object SET state='deleted' WHERE object_sha256=p_hash AND state='deleting';
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='OBJECT_NOT_DELETING',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_object_not_deleting'; END IF;
END $$;

CREATE FUNCTION chronicle.pin_snapshot(p_pin uuid,p_dataset text,p_snapshot text,p_worker uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    INSERT INTO chronicle.reader_pin(pin_id,dataset_id,snapshot_id,worker_id)
        VALUES(p_pin,p_dataset,p_snapshot,p_worker);
END $$;
CREATE FUNCTION chronicle.release_snapshot_pin(p_pin uuid,p_worker uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    UPDATE chronicle.reader_pin SET released_at=COALESCE(released_at,statement_timestamp())
        WHERE pin_id=p_pin AND worker_id=p_worker;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='PIN_NOT_OWNED',SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_pin_not_owned'; END IF;
END $$;

CREATE FUNCTION chronicle.reject_artifact_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='IMMUTABLE_ARTIFACT_RECORD';
END $$;
REVOKE ALL ON FUNCTION chronicle.reject_artifact_mutation() FROM PUBLIC;

-- Published and source evidence are insert-only, including for the owner.
DO $$ DECLARE n text; BEGIN
    FOREACH n IN ARRAY ARRAY['managed_dataset','rights_policy','rights_revocation','ticket_object','batch_artifact',
        'artifact_publication','snapshot_object'] LOOP
        EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON chronicle.%I '
            'FOR EACH ROW EXECUTE FUNCTION chronicle.reject_artifact_mutation()', n);
        EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON chronicle.%I '
            'FOR EACH STATEMENT EXECUTE FUNCTION chronicle.reject_artifact_mutation()', n);
    END LOOP;
END $$;
GRANT SELECT ON chronicle.managed_dataset,chronicle.rights_policy,chronicle.rights_revocation,chronicle.storage_object,
    chronicle.staging_ticket,chronicle.ticket_object,chronicle.batch_artifact,
    chronicle.artifact_publication,chronicle.snapshot_object,chronicle.reader_pin
    TO chronicle_reader,chronicle_publisher;
REVOKE ALL ON FUNCTION chronicle.object_lock_key(text),
    chronicle.open_object_ticket(uuid,text,uuid,text,jsonb),
    chronicle.register_object(uuid,uuid,text,bigint),chronicle.object_ready(uuid,uuid,text),
    chronicle.end_object_ticket(uuid,uuid,boolean),
    chronicle.attach_batch_artifact(text,text,uuid,uuid,text),
    chronicle.artifact_manifest(text,text,text,text[]),
    chronicle.publish_artifact_snapshot(uuid,text,text,text,text[],text,uuid,uuid,text),
    chronicle.revoke_rights(text,text),chronicle.begin_object_deletion(text,integer),
    chronicle.finish_object_deletion(text),chronicle.pin_snapshot(uuid,text,text,uuid),
    chronicle.release_snapshot_pin(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION chronicle.object_lock_key(text),
    chronicle.open_object_ticket(uuid,text,uuid,text,jsonb),
    chronicle.register_object(uuid,uuid,text,bigint),chronicle.object_ready(uuid,uuid,text),
    chronicle.end_object_ticket(uuid,uuid,boolean),
    chronicle.attach_batch_artifact(text,text,uuid,uuid,text),
    chronicle.artifact_manifest(text,text,text,text[]),
    chronicle.publish_artifact_snapshot(uuid,text,text,text,text[],text,uuid,uuid,text),
    chronicle.revoke_rights(text,text),chronicle.begin_object_deletion(text,integer),
    chronicle.finish_object_deletion(text) TO chronicle_publisher;
GRANT EXECUTE ON FUNCTION chronicle.pin_snapshot(uuid,text,text,uuid),
    chronicle.release_snapshot_pin(uuid,uuid) TO chronicle_reader,chronicle_publisher;

-- A managed dataset must use complete artifact publication. Keep legacy C1
-- synthetic datasets compatible without granting a bypass to the managed path.
ALTER FUNCTION chronicle.publish_snapshot(text,text,text[],text,text)
    RENAME TO publish_snapshot_legacy;
REVOKE ALL ON FUNCTION chronicle.publish_snapshot_legacy(text,text,text[],text,text)
    FROM PUBLIC,chronicle_publisher;
CREATE FUNCTION chronicle.publish_snapshot(
    p_dataset_id text,p_snapshot_id text,p_batch_ids text[],
    p_manifest_sha256 text,p_expected_current text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    PERFORM 1 FROM chronicle.dataset WHERE dataset_id=p_dataset_id FOR UPDATE;
    IF EXISTS(SELECT 1 FROM chronicle.managed_dataset WHERE dataset_id=p_dataset_id) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='ARTIFACT_PUBLICATION_REQUIRED',
            SCHEMA='chronicle',TABLE='storage_object',CONSTRAINT='object_artifact_publication_required';
    END IF;
    RETURN chronicle.publish_snapshot_legacy(
        p_dataset_id,p_snapshot_id,p_batch_ids,p_manifest_sha256,p_expected_current);
END $$;
REVOKE ALL ON FUNCTION chronicle.publish_snapshot(text,text,text[],text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION chronicle.publish_snapshot(text,text,text[],text,text)
    TO chronicle_publisher;

GRANT SELECT ON chronicle.snapshot,chronicle.snapshot_batch TO chronicle_publisher;
