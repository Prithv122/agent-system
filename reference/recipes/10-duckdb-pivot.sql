-- Question: How does 2026 revenue split across order statuses, month by month?
-- Pattern: Long-to-wide with DuckDB's PIVOT statement
-- DuckDB-specific: PIVOT is a DuckDB statement, not standard SQL. Snowflake and SQL
--   Server have their own incompatible spellings; Postgres has none without an
--   extension. The pivot columns are discovered from the data at plan time, which is
--   the real convenience -- and the real catch, because the result's shape then
--   depends on the data rather than on the query text.
-- Portable equivalent: recipes/11-portable-conditional-aggregation.sql. The two are
--   asserted to return identical results in tests/test_semantics.py.

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

SELECT *
FROM (PIVOT monthly ON status USING SUM(revenue) GROUP BY month)
ORDER BY month;
