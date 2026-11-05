-- All modes use the immutable context pin. This query never reads current again.
WITH context AS (
    SELECT c.* FROM chronicle.analysis_context AS c WHERE c.context_id = %(context_id)s
), members AS (
    SELECT r.*, sr.source_order, sr.availability_precision, sr.release_day
    FROM context AS c
    JOIN chronicle.revision AS r
        ON r.dataset_id = c.dataset_id
        AND r.source_namespace = c.source_namespace
        AND r.series_key = c.series_key
        AND r.observation_month = ANY(c.months)
    JOIN chronicle.source_release AS sr
        ON sr.dataset_id = r.dataset_id
        AND sr.source_namespace = r.source_namespace
        AND sr.source_revision_key = r.source_revision_key
    WHERE EXISTS (
        SELECT 1
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
    )
), cells AS (
    SELECT grid.month AS observation_month, grid.position,
        winner.value, winner.source_revision_key, winner.source_order,
        winner.unit_name, winner.metadata_version, winner.availability_precision,
        CASE
            WHEN /* F3_UNKNOWN_SHADOW_BEGIN */
                c.mode = 'source_as_of' AND unknown.source_order IS NOT NULL
                AND (winner.source_order IS NULL OR unknown.source_order > winner.source_order)
                /* F3_UNKNOWN_SHADOW_END */
                THEN 'unknown_availability'
            WHEN winner.source_order IS NULL THEN 'not_available_at_cutoff'
            WHEN c.expected_unit_name IS NOT NULL AND
                ROW(winner.unit_name, winner.metadata_version) IS DISTINCT FROM
                ROW(c.expected_unit_name, c.expected_metadata_version)
                THEN 'unit_mismatch'
            ELSE winner.status
        END AS status
    FROM context AS c
    CROSS JOIN LATERAL pg_catalog.unnest(c.months) WITH ORDINALITY AS grid(month, position)
    LEFT JOIN LATERAL (
        SELECT member.*
        FROM members AS member
        WHERE member.observation_month = grid.month
            AND (c.mode <> 'source_as_of' OR (
                member.availability_precision = 'date'
                AND member.release_day < c.exclusive_end_date
            ))
        ORDER BY member.source_order DESC
        LIMIT 1
    ) AS winner ON TRUE
    LEFT JOIN LATERAL (
        SELECT pg_catalog.max(member.source_order) AS source_order
        FROM members AS member
        WHERE member.observation_month = grid.month
            AND member.availability_precision = 'unknown'
    ) AS unknown ON TRUE
)
SELECT observation_month, status,
    CASE WHEN status = 'value' THEN value ELSE NULL END AS value,
    CASE WHEN status <> 'unknown_availability' THEN source_revision_key ELSE NULL END
        AS source_revision_key,
    CASE WHEN status <> 'unknown_availability' THEN source_order ELSE NULL END AS source_order,
    CASE WHEN status <> 'unknown_availability' THEN unit_name ELSE NULL END AS unit_name,
    CASE WHEN status <> 'unknown_availability' THEN metadata_version ELSE NULL END AS metadata_version,
    status = 'unknown_availability' OR COALESCE(availability_precision = 'unknown', FALSE)
        AS availability_unknown
FROM cells
ORDER BY position;
