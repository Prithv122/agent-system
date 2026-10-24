-- Question: What was daily completed revenue in April 2026, including the days with
--   no orders at all?
-- Pattern: Recursive CTE date spine + LEFT JOIN to zero-fill the gaps
-- Why it matters: GROUP BY over the fact table can only return days that exist in it.
--   A day with zero revenue is a row you have to manufacture, and forgetting to do so
--   is how a "revenue by day" chart quietly skips its worst days.
-- DuckDB note: generate_series(DATE '2026-04-01', DATE '2026-04-30', INTERVAL 1 DAY)
--   does the same thing in one line. The recursive form is here because it is what
--   you fall back to on an engine without a series generator.
-- Portable: DATE + INTEGER adds days in DuckDB; Postgres needs day + INTERVAL '1 day'.

WITH RECURSIVE spine AS (
    SELECT DATE '2026-04-01' AS day
    UNION ALL
    SELECT day + 1 FROM spine WHERE day < DATE '2026-04-30'
),

daily AS (
    SELECT
        CAST(o.order_ts AS DATE)         AS day,
        COUNT(DISTINCT o.order_id)       AS orders,
        SUM(oi.quantity * oi.unit_price) AS revenue
    FROM orders o
    JOIN order_items oi ON oi.order_id = o.order_id
    WHERE o.status = 'completed'
      AND o.order_ts >= DATE '2026-04-01'
      AND o.order_ts <  DATE '2026-05-01'
    GROUP BY CAST(o.order_ts AS DATE)
)

SELECT
    s.day,
    COALESCE(d.orders, 0)                       AS orders,
    COALESCE(d.revenue, CAST(0 AS DECIMAL(38,2))) AS revenue
FROM spine s
LEFT JOIN daily d ON d.day = s.day
ORDER BY s.day;
