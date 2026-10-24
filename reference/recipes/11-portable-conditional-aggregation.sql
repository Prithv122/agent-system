-- Question: How does 2026 revenue split across order statuses, month by month?
-- Pattern: Conditional aggregation -- SUM(CASE WHEN ...) once per output column
-- Note: the same question as recipe 10, answered without any PIVOT syntax.
-- Why it matters: this runs unchanged on every SQL engine, and unlike PIVOT the
--   result's column list is fixed by the query text rather than discovered from the
--   data. That is a downside when the categories are unknown, and an upside when a
--   downstream consumer expects a stable schema. Adding a fourth status would silently
--   add a column to recipe 10 and silently drop the revenue here -- which is the
--   tradeoff, stated plainly.
-- Portable: standard SQL.

WITH monthly AS (
    SELECT
        STRFTIME(o.order_ts, '%Y-%m')    AS month,
        o.status                         AS status,
        SUM(oi.quantity * oi.unit_price) AS revenue
    FROM orders o
    JOIN order_items oi ON oi.order_id = o.order_id
    WHERE o.order_ts >= DATE '2026-01-01'
    GROUP BY 1, 2
)

SELECT
    month,
    SUM(CASE WHEN status = 'cancelled' THEN revenue END) AS cancelled,
    SUM(CASE WHEN status = 'completed' THEN revenue END) AS completed,
    SUM(CASE WHEN status = 'returned'  THEN revenue END) AS returned
FROM monthly
GROUP BY month
ORDER BY month;
