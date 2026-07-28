-- =============================================================================
-- Final source segmentation — splits "other brands" into Word-of-Mouth vs
-- named Other brands, names each brand, and isolates the Website subs that
-- actually came from Meta. Week Jul 18–24, 2026, active. Refresh MV first.
-- =============================================================================

SELECT
    CASE
        WHEN source_label = 'Meta'    THEN '1. Meta'
        WHEN source_label = 'Taboola' THEN '2. Taboola'
        -- Website with a real Meta utm → actually Meta (source beat utm at L4>L5)
        WHEN source_label = 'Website'
             AND LOWER(TRIM(utm_source)) IN ('facebook','meta','fb','ig')
            THEN '3. Meta (mislabeled Website — utm=fb/ig)'
        -- Website with NO utm signal → direct / word of mouth
        WHEN source_label = 'Website'
             AND (COALESCE(TRIM(utm_source),'')='' OR LOWER(TRIM(utm_source)) IN ('none','null','(none)','(null)','-','n/a'))
            THEN '4. Word of Mouth / Direct'
        -- Website via an owned/email channel (e.g. Campaign Monitor) → other brands
        WHEN source_label = 'Website'
            THEN '5. Other brands (website via ' || LOWER(TRIM(utm_source)) || ')'
        WHEN source_label = 'Organic' THEN '7. Organic'
        WHEN source_label IS NULL     THEN '7. Organic'
        -- everything else = named other brand
        ELSE '6. Other brand: ' || source_label
    END AS segment,
    COUNT(*) AS subs
FROM superage.mv_subscriber_acquisition
WHERE date_joined >= '2026-07-18' AND date_joined < '2026-07-25'
  AND LOWER(TRIM(COALESCE(state,''))) = 'active'
GROUP BY 1
ORDER BY 1;
