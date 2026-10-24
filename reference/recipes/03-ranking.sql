-- Question: Within one category and one quarter, how do products rank by number of
--   completed orders -- and what happens to that ranking where products tie?
-- Pattern: ROW_NUMBER vs RANK vs DENSE_RANK over the same ORDER BY
-- Why it matters: the three functions differ only on ties, which is precisely when the
--   choice matters. This slice has two tie groups, so the difference is visible rather
--   than asserted: RANK skips numbers after a tie (1,2,3,3,5,5), DENSE_RANK does not
--   (1,2,3,3,4,4), and ROW_NUMBER always produces distinct numbers -- which means it
--   invents an order the data does not contain unless you break the tie yourself.
--   The product_id tiebreaker below is what makes this query reproducible; see
--   recipe 12 for what goes wrong without one.

WITH product_orders AS (
    SELECT
        p.product_id,
        p.name     AS product_name,
        p.category,
        COUNT(DISTINCT o.order_id) AS order_count
    FROM products p
    JOIN order_items oi ON oi.product_id = p.product_id
    JOIN orders o       ON o.order_id = oi.order_id
    WHERE o.status = 'completed'
      AND p.category = 'Audio'
      AND o.order_ts >= DATE '2026-01-01'
      AND o.order_ts <  DATE '2026-04-01'
    GROUP BY p.product_id, p.name, p.category
)

SELECT
    category,
    product_name,
    order_count,
    ROW_NUMBER() OVER (ORDER BY order_count DESC, product_id) AS row_number_tiebroken,
    RANK()       OVER (ORDER BY order_count DESC)             AS rank_with_gaps,
    DENSE_RANK() OVER (ORDER BY order_count DESC)             AS dense_rank_no_gaps
FROM product_orders
ORDER BY order_count DESC, product_id;
