-- Question: When several page views share one timestamp to the second, which came
--   "first" -- and how do you rank them without inventing an answer?
-- Pattern: Deterministic ranking under ties: a unique tiebreaker in the ORDER BY, plus
--   a COUNT(*) OVER that makes the ambiguity visible instead of silent
-- Why it matters: ROW_NUMBER() OVER (ORDER BY event_ts) is not stable when event_ts
--   repeats. Nothing errors; the query just picks a row, and may pick a different one
--   after a version upgrade, a parallelism change, or a reload of the same data. This
--   warehouse has 510 timestamps shared by more than one event for the same customer,
--   so "first event of the session" is genuinely undefined without a tiebreaker.
--   Adding event_id to the ORDER BY makes it total, and therefore reproducible.
-- The events_sharing_ts column is not decoration: surfacing the tie count is how you
--   find out whether your ordering key is unique before a dashboard depends on it.

WITH tie_counts AS (
    SELECT
        event_id,
        customer_id,
        event_ts,
        page,
        COUNT(*) OVER (PARTITION BY customer_id, event_ts) AS events_sharing_ts
    FROM page_events
)

SELECT
    customer_id,
    event_ts,
    events_sharing_ts,
    page,
    event_id,
    ROW_NUMBER() OVER (
        PARTITION BY customer_id, event_ts
        ORDER BY event_id          -- the tiebreaker that makes this reproducible
    ) AS tie_safe_rank
FROM tie_counts
WHERE events_sharing_ts > 1
ORDER BY customer_id, event_ts, event_id
LIMIT 20;
