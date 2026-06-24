-- Pipeline distribution: how many roles sit at each status for one operator.
-- Ordered by the real funnel sequence (new -> ... -> offered, with 'not a fit'
-- parked at the end) rather than alphabetically, so the report reads top of
-- funnel to bottom the way a human reasons about a pipeline.
WITH funnel_order(status, ord) AS (
    VALUES ('new', 0), ('good fit', 1), ('materials pending', 2),
           ('submitted', 3), ('interviewed', 4), ('offered', 5),
           ('not a fit', 9)
),
counts AS (
    SELECT status, COUNT(*) AS n
    FROM roles
    WHERE user_id = :uid
    GROUP BY status
)
SELECT c.status, c.n
FROM counts c
LEFT JOIN funnel_order f ON f.status = c.status
ORDER BY COALESCE(f.ord, 99), c.status
