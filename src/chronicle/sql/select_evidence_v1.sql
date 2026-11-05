-- Evidence v1: the membership relation both admits a revision and supplies its
-- receipts, so no later/unrelated occurrence can be attached to an earlier pin.
WITH context AS (
    SELECT c.* FROM chronicle.analysis_context AS c WHERE c.context_id = %(context_id)s
), members AS (
    SELECT r.*, release.source_order, release.availability_precision, release.release_day,
        pg_catalog.jsonb_build_object(
            'dataset_id', r.dataset_id,
            'snapshot_id', c.snapshot_id,
            'source_namespace', r.source_namespace,
            'series_key', r.series_key,
            'observation_month', pg_catalog.to_char(r.observation_month, 'YYYY-MM-DD'),
            'source_revision_key', r.source_revision_key,
            'source_order', release.source_order,
            'availability', pg_catalog.jsonb_build_object(
                'precision', release.availability_precision,
                'date', pg_catalog.to_char(release.release_day, 'YYYY-MM-DD')),
            'source_status', r.status,
            'lexical_value', r.value,
            'unit', pg_catalog.jsonb_build_object(
                'name', r.unit_name, 'metadata_version', r.metadata_version),
            'occurrences', receipts.occurrences
        ) AS evidence
    FROM context AS c
    JOIN chronicle.revision AS r
        ON r.dataset_id = c.dataset_id
        AND r.source_namespace = c.source_namespace
        AND r.series_key = c.series_key
        AND r.observation_month = ANY(c.months)
    JOIN chronicle.source_release AS release
        ON release.dataset_id = r.dataset_id
        AND release.source_namespace = r.source_namespace
        AND release.source_revision_key = r.source_revision_key
    JOIN LATERAL (
        SELECT pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'dataset_id', bounded.dataset_id,
                'snapshot_id', bounded.snapshot_id,
                'source_namespace', bounded.source_namespace,
                'series_key', bounded.series_key,
                'observation_month', pg_catalog.to_char(bounded.observation_month, 'YYYY-MM-DD'),
                'source_revision_key', bounded.source_revision_key,
                'batch_id', bounded.batch_id,
                'received_at', pg_catalog.to_char(
                    bounded.received_at AT TIME ZONE 'UTC',
                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
            ) ORDER BY bounded.batch_id COLLATE "C", bounded.received_at
        ) AS occurrences
        FROM (
            -- Scope first, then fetch one row beyond the service limit. The
            -- Python boundary rejects overflow; this is never truncated success.
            -- LIMIT precedes JSON aggregation so the aggregate cannot allocate
            -- an unbounded occurrence array before the boundary can reject it.
            SELECT occurrence.*, membership.snapshot_id
            FROM chronicle.revision_occurrence AS occurrence
            JOIN chronicle.snapshot_batch AS membership
                ON membership.dataset_id = occurrence.dataset_id
                AND membership.batch_id = occurrence.batch_id
                AND membership.snapshot_id = c.snapshot_id
            WHERE occurrence.dataset_id = r.dataset_id
                AND occurrence.source_namespace = r.source_namespace
                AND occurrence.series_key = r.series_key
                AND occurrence.observation_month = r.observation_month
                AND occurrence.source_revision_key = r.source_revision_key
            ORDER BY occurrence.batch_id COLLATE "C", occurrence.received_at
            LIMIT %(occurrence_limit)s
        ) AS bounded
    ) AS receipts ON receipts.occurrences IS NOT NULL
), candidates AS (
    SELECT c.*, grid.month AS observation_month, grid.position,
        winner.status AS source_status, winner.value AS lexical_value,
        winner.unit_name, winner.metadata_version, winner.availability_precision,
        winner.evidence AS winner_evidence, unknown.evidence AS unknown_evidence,
        winner.source_order,
        c.mode = 'source_as_of' AND unknown.source_order IS NOT NULL
            AND (winner.source_order IS NULL OR unknown.source_order > winner.source_order)
            AS blocked,
        c.expected_unit_name IS NULL OR
            ROW(winner.unit_name, winner.metadata_version) =
            ROW(c.expected_unit_name, c.expected_metadata_version) AS unit_compatible
    FROM context AS c
    CROSS JOIN LATERAL pg_catalog.unnest(c.months) WITH ORDINALITY AS grid(month, position)
    LEFT JOIN LATERAL (
        SELECT member.* FROM members AS member
        WHERE member.observation_month = grid.month
            AND (c.mode <> 'source_as_of' OR (
                member.availability_precision = 'date'
                AND member.release_day < c.exclusive_end_date))
        ORDER BY member.source_order DESC
        LIMIT 1
    ) AS winner ON TRUE
    LEFT JOIN LATERAL (
        SELECT member.* FROM members AS member
        WHERE member.observation_month = grid.month
            AND member.availability_precision = 'unknown'
        ORDER BY member.source_order DESC
        LIMIT 1
    ) AS unknown ON TRUE
), cells AS (
    SELECT *, CASE
        WHEN blocked THEN 'unknown_availability'
        WHEN source_order IS NULL THEN 'not_available_at_cutoff'
        WHEN NOT unit_compatible THEN 'unit_mismatch'
        ELSE source_status
    END AS status
    FROM candidates
)
SELECT 'chronicle.evidence.v1' AS evidence_schema,
    dataset_id, snapshot_id, source_namespace, series_key, observation_month,
    mode, exclusive_end_date, status,
    CASE WHEN status = 'value' THEN lexical_value ELSE NULL END AS value,
    CASE WHEN NOT blocked THEN source_status ELSE NULL END AS source_status,
    CASE WHEN NOT blocked AND source_order IS NOT NULL THEN unit_compatible ELSE NULL END
        AS unit_compatible,
    CASE WHEN NOT blocked AND source_order IS NOT NULL THEN
        pg_catalog.jsonb_build_object('name', unit_name, 'metadata_version', metadata_version)
        ELSE NULL END AS unit,
    blocked OR COALESCE(availability_precision = 'unknown', FALSE) AS availability_unknown,
    CASE WHEN NOT blocked THEN winner_evidence ELSE NULL END AS selected_revision,
    CASE WHEN blocked THEN unknown_evidence ELSE NULL END AS blocking_revision
FROM cells
ORDER BY position;
