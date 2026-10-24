-- Question: How long does each customer wait between orders, and when is the next one?
-- Pattern: LAG / LEAD over a reusable named WINDOW
-- Why it matters: LAG and LEAD are how you turn a table of events into a table of
--   intervals without a self-join. The named WINDOW clause keeps the partition
--   definition in one place, so the three columns below cannot drift apart.

WITH completed_orders AS (
    SELECT
        o.order_id,
        o.customer_id,
        CAST(o.order_ts AS DATE) AS order_date
    FROM orders o
    WHERE o.status = 'completed'
),

sample_customers AS (
    SELECT customer_id
    FROM completed_orders
    GROUP BY customer_id
    HAVING COUNT(*) BETWEEN 5 AND 7
    ORDER BY customer_id
    LIMIT 3
)

SELECT
    co.customer_id,
    co.order_id,
    co.order_date,
    LAG(co.order_date)  OVER w AS prev_order_date,
    LEAD(co.order_date) OVER w AS next_order_date,
    DATE_DIFF('day', LAG(co.order_date) OVER w, co.order_date) AS days_since_prev
FROM completed_orders co
JOIN sample_customers sc ON sc.customer_id = co.customer_id
WINDOW w AS (PARTITION BY co.customer_id ORDER BY co.order_date, co.order_id)
ORDER BY co.customer_id, co.order_date, co.order_id;
