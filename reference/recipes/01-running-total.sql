-- Question: How does each customer's completed-order spend accumulate over time?
-- Pattern: Running total with SUM(...) OVER (PARTITION BY ... ORDER BY ...)
-- Benchmark: benchmarks/01-running-total
-- Why it matters: the frame clause, not the ORDER BY, is what makes this a running
--   total. Drop "ROWS BETWEEN ..." and the default frame is RANGE, which sums every
--   peer row sharing the same ORDER BY value -- silently wrong the moment two orders
--   land on the same day.

WITH order_value AS (
    SELECT
        o.order_id,
        o.customer_id,
        CAST(o.order_ts AS DATE)         AS order_date,
        SUM(oi.quantity * oi.unit_price) AS order_value
    FROM orders o
    JOIN order_items oi ON oi.order_id = o.order_id
    WHERE o.status = 'completed'
    GROUP BY o.order_id, o.customer_id, o.order_ts
),

-- Three mid-sized customers, so the printed result stays readable.
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
    ov.order_date,
    ov.order_value,
    SUM(ov.order_value) OVER (
        PARTITION BY ov.customer_id
        ORDER BY ov.order_date, ov.order_id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_total
FROM order_value ov
JOIN sample_customers sc ON sc.customer_id = ov.customer_id
ORDER BY ov.customer_id, ov.order_date, ov.order_id;
