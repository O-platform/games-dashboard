-- =============================================================================
-- Validate the WEBSITE label — do these subs carry a competing chain signal
-- that makes 'Website' wrong? Week Jul 18–24, 2026, active. Refresh MV first.
--
-- Chain priority: L1 acq > L2 url_vars(meta) > L3 sub_source > L4 source > L5 utm.
-- Website is only reached at L4 (source=website), so:
--   • a real signal at acq / url_vars=meta / sub_source  => would have won EARLIER
--     => if any Website sub shows one, that's a genuine ordering bug (flagged
--        "unexpected" below).
--   • utm_source (L5) sits BELOW source, so utm=facebook losing to source=website
--     is the only "arguably wrong" case (the edge cases).
-- =============================================================================

-- 1. Chain check — bucket every Website sub by whether it hides a Meta signal
SELECT
    CASE
        WHEN LOWER(TRIM(acquisition_utm_source)) IN ('facebook','meta','fb','ig')
            THEN 'BUG: acq_utm is Meta (L1 should have won)'
        WHEN LOWER(TRIM(SUBSTRING(url_variables FROM 'utm_source=([^,&]+)'))) = 'meta'
            THEN 'BUG: url_variables=meta (L2 should have won)'
        WHEN LOWER(TRIM(sub_source)) IN ('facebook','meta','fb','ig')
            THEN 'BUG: sub_source is Meta (L3 should have won)'
        WHEN LOWER(TRIM(utm_source)) IN ('facebook','meta','fb','ig')
            THEN 'EDGE: utm_source is Meta but source=website won (L4 > L5)'
        ELSE 'OK: no competing Meta signal — Website is correct'
    END AS chain_check,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label = 'Website'
GROUP BY 1
ORDER BY subs DESC;


-- 2. The full raw chain for every Website sub (so you can eyeball what else is set)
SELECT
    COALESCE(NULLIF(TRIM(acquisition_utm_source),''),'(none)') AS acq_utm,
    COALESCE(NULLIF(TRIM(sub_source),''),'(none)')             AS sub_source,
    COALESCE(NULLIF(TRIM(source),''),'(none)')                 AS source,
    COALESCE(NULLIF(TRIM(utm_source),''),'(none)')             AS utm_source,
    COALESCE(NULLIF(TRIM(SUBSTRING(url_variables FROM 'utm_source=([^,&]+)')),''),'(no utm in url)') AS url_vars_utm,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label = 'Website'
GROUP BY 1,2,3,4,5
ORDER BY subs DESC;
