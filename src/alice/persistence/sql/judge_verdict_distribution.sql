-- Judge-drift monitor: per judge model, the verdict mix and how often the
-- consistency (second-pass) check agreed with the first verdict. A falling
-- agreement rate means the model is drifting and the rubric needs recalibration;
-- this is why fit_verdicts keeps every run instead of overwriting a role's score.
SELECT
    judge_model,
    COUNT(*)                                     AS verdicts,
    COUNT(*) FILTER (WHERE verdict = 'FIT')      AS fit,
    COUNT(*) FILTER (WHERE verdict = 'NOT-FIT')  AS not_fit,
    COUNT(*) FILTER (WHERE verdict = 'REACH')    AS reach,
    ROUND(100.0 * COUNT(*) FILTER (WHERE consistent) / NULLIF(COUNT(*) FILTER (WHERE consistent IS NOT NULL), 0), 1) AS agreement_pct
FROM fit_verdicts
WHERE user_id = :uid
GROUP BY judge_model
ORDER BY verdicts DESC
