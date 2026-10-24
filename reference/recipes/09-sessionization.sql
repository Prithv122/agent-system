-- Question: How do raw page views break into browsing sessions under a 30-minute
--   inactivity rule?
-- Pattern: LAG to measure the gap, a 0/1 boundary flag, then a cumulative SUM of that
--   flag to number the sessions
-- Benchmark: benchmarks/09-sessionization
-- Why it matters: the cumulative sum of a boundary flag is the general way to turn
--   "when does a new group start" into a group id, and it generalises far past
--   sessionization. Note the boundary rule is STRICTLY greater than 30 minutes, so a
--   gap of exactly 30 minutes continues the session. Customer 7 has exactly such a
--   gap, on purpose, so the choice is pinned down by a test rather than by opinion.

WITH flagged AS (
    SELECT
        event_id,
        customer_id,
        event_ts,
        CASE
            WHEN LAG(event_ts) OVER w IS NULL THEN 1
            WHEN event_ts - LAG(event_ts) OVER w > INTERVAL 30 MINUTE THEN 1
            ELSE 0
        END AS starts_session
    FROM page_events
    WINDOW w AS (PARTITION BY customer_id ORDER BY event_ts, event_id)
),

numbered AS (
    SELECT
        *,
        SUM(starts_session) OVER (
            PARTITION BY customer_id
            ORDER BY event_ts, event_id
            ROWS UNBOUNDED PRECEDING
        ) AS session_number
    FROM flagged
)

SELECT
    customer_id,
    session_number,
    MIN(event_ts) AS session_start,
    MAX(event_ts) AS session_end,
    COUNT(*)      AS events,
    DATE_DIFF('second', MIN(event_ts), MAX(event_ts)) AS duration_seconds
FROM numbered
WHERE customer_id IN (1, 2, 3, 7)
GROUP BY customer_id, session_number
ORDER BY customer_id, session_number;
