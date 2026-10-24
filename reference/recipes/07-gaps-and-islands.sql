-- Question: What are the longest streaks of consecutive days on which a customer
--   visited the site?
-- Pattern: Gaps-and-islands via the date minus ROW_NUMBER trick
-- Why it matters: for a run of consecutive dates, date - row_number() is constant,
--   because both increase by exactly one per row. That constant becomes the group key.
--   The whole technique is one line; recognising where it applies is the skill.
-- Portable: standard SQL. DuckDB spells the date-minus-integer cast as CAST(... AS INTEGER).

WITH active_days AS (
    SELECT DISTINCT
        customer_id,
        CAST(event_ts AS DATE) AS active_date
    FROM page_events
),

keyed AS (
    SELECT
        customer_id,
        active_date,
        active_date - CAST(
            ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY active_date) AS INTEGER
        ) AS island_key
    FROM active_days
),

islands AS (
    SELECT
        customer_id,
        MIN(active_date) AS streak_start,
        MAX(active_date) AS streak_end,
        COUNT(*)         AS streak_days
    FROM keyed
    GROUP BY customer_id, island_key
)

SELECT customer_id, streak_start, streak_end, streak_days
FROM islands
WHERE streak_days >= 2
ORDER BY streak_days DESC, customer_id, streak_start
LIMIT 15;
