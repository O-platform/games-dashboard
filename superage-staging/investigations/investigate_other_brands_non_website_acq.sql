-- =============================================================================
-- Other-brands (corrected) EXCLUDING Meta & Website — the raw acq values
-- Week Jul 18–24, 2026, active. Refresh MV first.
-- Answers: "beside Meta and Website, what are the other acq values?"
-- =============================================================================

-- 1. The ~52 non-Website other-brands, by canonical label + raw fields
SELECT
    source_label,
    COALESCE(NULLIF(TRIM(acquisition_utm_source),''),'(none)') AS acq_utm,
    COALESCE(NULLIF(TRIM(sub_source),''),'(none)')             AS sub_source,
    COALESCE(NULLIF(TRIM(source),''),'(none)')                 AS source,
    COALESCE(NULLIF(TRIM(utm_source),''),'(none)')             AS utm_source,
    COALESCE(NULLIF(TRIM(url_variables),''),'(none)')          AS url_variables,
    COUNT(*)                                                    AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label NOT IN ('Meta','Taboola','Organic','Website')
  AND source_label IS NOT NULL
GROUP BY 1,2,3,4,5,6
ORDER BY subs DESC;


-- 2. Same set, collapsed to just the canonical label + count (the clean summary)
SELECT source_label, COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
  AND source_label NOT IN ('Meta','Taboola','Organic','Website')
  AND source_label IS NOT NULL
GROUP BY 1
ORDER BY 2 DESC;
