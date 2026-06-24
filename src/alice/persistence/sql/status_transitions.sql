-- Status-transition frequency: which status changes actually happen, and how
-- often. LEAD walks each role's history in chronological order so consecutive
-- (from -> to) pairs fall out without a self-join; the result shows where the
-- pipeline really flows (and where it stalls or skips stages).
WITH transitions AS (
    SELECT
        status AS from_status,
        LEAD(status) OVER (PARTITION BY role_id ORDER BY changed_at, id) AS to_status
    FROM status_history
    WHERE user_id = :uid
)
SELECT from_status, to_status, COUNT(*) AS n
FROM transitions
WHERE to_status IS NOT NULL
GROUP BY from_status, to_status
ORDER BY n DESC, from_status, to_status
