-- Question: Which customers let their subscription lapse, and for how many days?
-- Pattern: Interval merging (a gaps-and-islands variant) with a running MAX, then
--   LEAD across the merged intervals to measure the gaps between them
-- Why it matters: renewals in this data overlap, abut, and lapse. Comparing each
--   period only to the one before it gets the overlapping case wrong -- a short period
--   fully contained in a longer one would look like a lapse. The running MAX of every
--   *prior* end date is what makes the merge correct.
-- Note the frame: UNBOUNDED PRECEDING AND 1 PRECEDING deliberately excludes the
--   current row, so a period is compared against its predecessors, not itself.

WITH bounded AS (
    SELECT
        customer_id,
        period_start,
        period_end,
        MAX(period_end) OVER (
            PARTITION BY customer_id
            ORDER BY period_start, period_end
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS prior_max_end
    FROM subscriptions
),

flagged AS (
    SELECT
        *,
        CASE
            WHEN prior_max_end IS NULL OR period_start > prior_max_end THEN 1
            ELSE 0
        END AS starts_new_island
    FROM bounded
),

islands AS (
    SELECT
        *,
        SUM(starts_new_island) OVER (
            PARTITION BY customer_id
            ORDER BY period_start, period_end
            ROWS UNBOUNDED PRECEDING
        ) AS island_id
    FROM flagged
),

covered AS (
    SELECT
        customer_id,
        island_id,
        MIN(period_start) AS covered_from,
        MAX(period_end)   AS covered_to
    FROM islands
    GROUP BY customer_id, island_id
)

SELECT
    customer_id,
    covered_to AS lapsed_on,
    LEAD(covered_from) OVER (PARTITION BY customer_id ORDER BY island_id) AS resumed_on,
    DATE_DIFF(
        'day',
        covered_to,
        LEAD(covered_from) OVER (PARTITION BY customer_id ORDER BY island_id)
    ) AS lapse_days
FROM covered
QUALIFY resumed_on IS NOT NULL
ORDER BY lapse_days DESC, customer_id
LIMIT 15;
