-- Question: What are each customer's three largest completed orders?
-- Pattern: Top-N per group -- ROW_NUMBER in a window, filtered with QUALIFY
-- Benchmark: benchmarks/04-top-n-per-group
-- DuckDB-specific: QUALIFY (also Snowflake, BigQuery, Teradata; NOT Postgres or MySQL)
-- Portable alternative: benchmarks/04-top-n-per-group-naive.sql, which reaches the
--   same answer with a correlated subquery and no window function at all.
-- Why it matters: you cannot put a window function in WHERE, because WHERE runs
--   before the window is computed. QUALIFY is the clause that filters *after* it.

WITH order_value AS (
    SELECT
        o.order_id,
        o.customer_id,
        SUM(oi.quantity * oi.unit_price) AS order_value
    FROM orders o
    JOIN order_items oi ON oi.order_id = o.order_id
    WHERE o.status = 'completed'
    GROUP BY o.order_id, o.customer_id
),

sample_customers AS (
    SELECT customer_id
    FROM order_value
    GROUP BY customer_id
    HAVING COUNT(*) BETWEEN 5 AND 7
    ORDER BY customer_id
    LIMIT 3
)

SELECT
    ov.customer_id,
    ov.order_id,
    ov.order_value,
    ROW_NUMBER() OVER (
        PARTITION BY ov.customer_id
        ORDER BY ov.order_value DESC, ov.order_id
    ) AS value_rank
FROM order_value ov
JOIN sample_customers sc ON sc.customer_id = ov.customer_id
QUALIFY value_rank <= 3
ORDER BY ov.customer_id, value_rank;
