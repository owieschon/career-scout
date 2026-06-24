-- Company-level suppression signal: per company, how many roles the operator
-- marked 'not a fit' vs 'good fit'. The sourcing pipeline uses this to suppress
-- companies it has repeatedly rejected. A single pass with conditional
-- aggregation computes both counts without scanning every row in Python.
SELECT
    company,
    COUNT(*) FILTER (WHERE status = 'not a fit') AS not_fit,
    COUNT(*) FILTER (WHERE status = 'good fit')  AS good_fit,
    COUNT(*)                                     AS total
FROM roles
WHERE user_id = :uid
  AND company IS NOT NULL
  AND company <> ''
GROUP BY company
HAVING COUNT(*) FILTER (WHERE status IN ('not a fit', 'good fit')) > 0
ORDER BY not_fit DESC, good_fit DESC, company
