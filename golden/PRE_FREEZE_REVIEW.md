# Pre-freeze review report — golden set and evaluation configuration

**Date:** 2026-10-25 · **Status: CONFIRMED by the owner 2026-10-25 as written; golden set frozen and EVAL_PLAN signed
before any real model call on the golden set.** Final choices: C = 3 attempts (2 repairs),
`employees.title` kept in the hints, unparseable replies keep repair feedback.

## 1 Edits made after the owner's review

| Item | Before | After | Why |
|---|---|---|---|
| sessionization-1 | 30-minute rule, customer 7 only: 1 row (sessions, page views) | 10-minute rule, customers 3, 26 and 127: 3 rows (customer id, sessions, page views) = (3, 35, 67), (26, 17, 29), (127, 16, 29) | The 30-minute rule only counted visits: one planted row was the only boundary case |
| sessionization-2 | 30-minute rule over all customers | 10-minute rule over all customers: 6,923 sessions, 1.78 views per session (raw 1.7768) | Same |
| `column_order_matters` | not present | true on 34 of 36 questions (every question that lists its output columns, every pivot) | Owner decision; free column order accepted swapped same-typed columns |
| The other 34 questions | — | text and reference SQL unchanged (checked field by field) | Owner decision |
| Shared warehouse | sql-cookbook generator, seed 20260830 | **unchanged** | Provenance kept; the new threshold sits inside the data's intra-visit gap range instead |

Why 10 minutes: inside a visit page views are 5 s to 20 min apart and visits are hours apart. At 10
minutes the data holds **5,370 gaps below, 8 exactly at (8 different customers) and 6,523 above** the
boundary. Each of customers 3, 26 and 127 has one exact-10-minute gap and 16+ sessions, so every row
of sessionization-1 changes (36 / 18 / 17) under the wrong rule ("10 minutes or more"), and
sessionization-2 becomes 6,931 sessions and 1.77.

## 2 Verification results (all on the current files)

| Check | Tool | Result |
|---|---|---|
| Guard + `EXPLAIN` accept every reference | `analyst.validator.validate` | 36/36 accepted |
| Read-only run, 1-999 rows, deterministic over two runs, non-degenerate (not all NULL, not all zero, not identical rows) | `analyst golden` | 36 questions OK |
| Independent recompute in plain Python over raw tables, compared with the scorer's equality rule | `golden/crosscheck.py` (in the test suite) | 36/36 match |
| Scorer audit: 575 rewritten variants of the 36 real results | `golden/audit_scorer.py` (in the test suite) | 291/291 expected-equal held, 284/284 expected-different held, 0 surprises |

What the scorer audit verified, as asked:

- **NULL:** NULL equals only NULL; NULL vs 0 and 0 vs NULL are different (lag_lead-1 has a real NULL).
- **DATE / TIMESTAMP:** a DATE equals the same day as a midnight TIMESTAMP and as ISO text (8/8 date
  columns). A question asking for "YYYY-MM text" is not met by a full date, by design.
- **Numeric tolerance:** DECIMAL, INTEGER, BIGINT, HUGEINT and DOUBLE re-typings compare equal;
  a 1e-9 relative change is equal; a 1e-4 relative change on the largest cell, or +1 on an integer,
  is different. Numbers returned as text are different (no coercion of VARCHAR).
- **Row order:** shuffled rows are equal only where `order_matters` is false (24/24); two swapped rows
  are different where it is true (11/11).
- **Column order:** two swapped same-typed columns are different (16/16) on questions that list their
  columns; a dropped, extra or NULLed column is always different. Row order and column order are
  independent rules.

## 3 Final configuration to confirm

| Setting | Value |
|---|---|
| Model | `qwen-coder-7b` = Ollama `qwen2.5-coder:7b-instruct-q3_K_M`, digest `bb739a02927ef1dd238b0fa05813c8fe1d87cc793abdfaa30349cf7265135ecf` |
| Generation | temperature 0 (greedy), writer `max_tokens` 1024, JSON mode, context window 8192, no seed sent (irrelevant under greedy decoding) |
| Value hints | on for every arm, mechanical rule: text columns with <= 12 distinct non-null values. Current set (sha256 `31aa2ee4…71e7c`): `customers.country`, `customers.segment`, `employees.title`, `orders.status`, `page_events.page`, `products.category`, `subscriptions.plan` |
| Arms | A = 1 attempt (no repair), B = 2 attempts (1 repair), C = 3 attempts (2 repairs); identical first prompt; feedback only from `EXPLAIN`, the engine, or "no query in the reply"; the writer never sees the reference |
| Primary hypothesis | EX(C) − EX(A) > 0 with the 95% paired bootstrap CI (10,000 resamples, seed 20260830) excluding 0 |
| Practical target | EX(C) − EX(A) >= +10 points, reported separately |
| Also reported | exact McNemar p (A vs B, A vs C), per-pattern EX, failure kinds, attempts, calls, tokens, latency |
| Run procedure | arms sequential (lock), A then B then C; sidecar pins model config, golden hash, hint hash; cache exported to `results/cache/llm.jsonl` |

## 4 Open questions for the owner

1. **Arm C's budget.** The approved brief (and this plan) define C as **3 attempts = 2 repairs**. The
   second review note described C as "up to three repairs" (4 attempts). Recommended: keep the brief's
   definition (A=1, B=2, C=3 attempts); a 4-attempt arm D can be added as a secondary analysis at the
   cost of at most 36 more writer calls, since the cache nests it too.
2. **`employees.title` in the hints** includes `CEO` and `VP`. recursive_hierarchy-2 asks for each
   analyst's VP; the hint makes "join on title = 'VP'" easy, but the reference finds the VP by the
   reporting chain and the crosscheck confirms both give the same answer here. Keep it (the rule is
   mechanical) unless you want a cardinality cut-off that excludes it.
3. Repairs also trigger on an unparseable reply ("the reply did not contain a query"). This is
   deterministic protocol feedback, not a validator error; recommended to keep, applied to every arm.

Once confirmed: `analyst golden --freeze`, tick and sign `EVAL_PLAN.md`, commit, then run A, B, C.

## Appendix — all 36 questions with reference SQL

### running_total-1 (easy, window, join)

For each calendar month of 2025, show the month as text in YYYY-MM form, that month's revenue, and the cumulative revenue from January 2025 through that month. Revenue means quantity times unit price added up over order lines, counting completed orders only. An order belongs to the month of its order timestamp. List the months in calendar order.

*12 rows x 3 columns; row order matters; column order matters; example (may appear in tests)*

```sql
WITH m AS ( SELECT strftime(o.order_ts, '%Y-%m') AS month, SUM(oi.quantity * oi.unit_price) AS revenue FROM orders o JOIN order_items oi ON oi.order_id = o.order_id WHERE o.status = 'completed' AND o.order_ts >= DATE '2025-01-01' AND o.order_ts < DATE '2026-01-01' GROUP BY 1) SELECT month, revenue, SUM(revenue) OVER (ORDER BY month ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_revenue FROM m ORDER BY month
```

First rows: (2025-01, 168134.07, 168134.07); (2025-02, 137864.78, 305998.85); (2025-03, 168484.46, 474483.31); ...

### running_total-2 (medium, window, join)

For each country and each quarter of 2025, show the country code, the quarter as text like 2025-Q3, the revenue from customers of that country in that quarter, and the running total of that country's revenue from 2025-Q1 up to and including that quarter. Revenue means quantity times unit price added up over order lines, counting completed orders only. A customer's country is the one stored on the customer record, and an order belongs to the quarter of its order timestamp. List by country code, then quarter.

*24 rows x 4 columns; row order matters; column order matters*

```sql
WITH q AS ( SELECT c.country, CAST(year(o.order_ts) AS VARCHAR) || '-Q' || CAST(quarter(o.order_ts) AS VARCHAR) AS quarter, SUM(oi.quantity * oi.unit_price) AS revenue FROM orders o JOIN order_items oi ON oi.order_id = o.order_id JOIN customers c ON c.customer_id = o.customer_id WHERE o.status = 'completed' AND o.order_ts >= DATE '2025-01-01' AND o.order_ts < DATE '2026-01-01' GROUP BY c.country, quarter) SELECT country, quarter, revenue, SUM(revenue) OVER (PARTITION BY country ORDER BY quarter ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_revenue FROM q ORDER BY country, quarter
```

First rows: (AU, 2025-Q1, 10261.85, 10261.85); (AU, 2025-Q2, 26901.10, 37162.95); (AU, 2025-Q3, 17092.34, 54255.29); ...

### running_total-3 (hard, window, cte)

Find the customers whose total spend on completed orders has reached 50,000 or more. For each of them, report the customer id, the id of the order that first took their cumulative spend to 50,000 or more, and their cumulative spend right after that order. Spend on an order is quantity times unit price added up over its lines, counting completed orders only. A customer's orders are taken in the order they were placed (by order timestamp; if two share a timestamp, the lower order id comes first).

*5 rows x 3 columns; column order matters*

```sql
WITH ov AS ( SELECT o.order_id, o.customer_id, o.order_ts, SUM(oi.quantity * oi.unit_price) AS order_value FROM orders o JOIN order_items oi ON oi.order_id = o.order_id WHERE o.status = 'completed' GROUP BY o.order_id, o.customer_id, o.order_ts), c AS ( SELECT customer_id, order_id, order_value, SUM(order_value) OVER (PARTITION BY customer_id ORDER BY order_ts, order_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_spend FROM ov) SELECT customer_id, order_id, cumulative_spend FROM c WHERE cumulative_spend >= 50000 AND cumulative_spend - order_value < 50000
```

First rows: (189, 534, 50491.74); (235, 2934, 50344.18); (247, 653, 50594.68); ...

### lag_lead-1 (easy, window)

For customer 13, list every completed order with its order id, its order date, and the number of days since that customer's previous completed order (leave it empty for the first one). Days are the difference between calendar dates. Order the list by order timestamp, and by order id if two share a timestamp.

*7 rows x 3 columns; row order matters; column order matters*

```sql
SELECT order_id, CAST(order_ts AS DATE) AS order_date, date_diff('day', LAG(CAST(order_ts AS DATE)) OVER (ORDER BY order_ts, order_id), CAST(order_ts AS DATE)) AS days_since_previous FROM orders WHERE customer_id = 13 AND status = 'completed' ORDER BY order_ts, order_id
```

First rows: (2638, 2024-07-24, None); (2296, 2024-08-09, 16); (1217, 2024-12-27, 140); ...

### lag_lead-2 (medium, window, join)

For each customer segment, what is the average number of days between a customer's consecutive completed orders? Compare each completed order with the same customer's immediately preceding completed order (by order timestamp, then order id), measure the gap as the difference between calendar dates, ignore each customer's first order, and average over all such gaps in the segment. Return the segment and the average rounded to 2 decimals.

*3 rows x 2 columns; column order matters*

```sql
WITH g AS ( SELECT customer_id, date_diff('day', LAG(CAST(order_ts AS DATE)) OVER (PARTITION BY customer_id ORDER BY order_ts, order_id), CAST(order_ts AS DATE)) AS gap FROM orders WHERE status = 'completed') SELECT c.segment, ROUND(AVG(g.gap), 2) AS avg_gap_days FROM g JOIN customers c ON c.customer_id = g.customer_id WHERE g.gap IS NOT NULL GROUP BY c.segment
```

First rows: (smb, 85.77); (consumer, 73.0); (enterprise, 84.56)

### lag_lead-3 (hard, window, join)

For each country, what percentage of completed orders were followed by another completed order from the same customer within 30 days? An order counts as followed if the customer's next completed order (by order timestamp, then order id) has a calendar date no more than 30 days after this order's date; a customer's last completed order is not followed. Use a customer's country from the customer record. Return the country code and the percentage of that country's completed orders that were followed, rounded to 1 decimal.

*6 rows x 2 columns; column order matters*

```sql
WITH n AS ( SELECT customer_id, CAST(order_ts AS DATE) AS d, LEAD(CAST(order_ts AS DATE)) OVER (PARTITION BY customer_id ORDER BY order_ts, order_id) AS next_d FROM orders WHERE status = 'completed') SELECT c.country, ROUND(100.0 * SUM(CASE WHEN date_diff('day', n.d, n.next_d) <= 30 THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_followed FROM n JOIN customers c ON c.customer_id = n.customer_id GROUP BY c.country
```

First rows: (AU, 17.4); (IN, 31.3); (US, 30.9); ...

### ranking-1 (easy, window)

Rank the products by total units sold on completed orders (quantity added up over order lines), most units first, where products with equal units share the same rank and the next rank is skipped (1, 2, 2, 4). Return the product id, its units sold and its rank for every product ranked 10th or better. Products with no completed sales are ignored.

*10 rows x 3 columns; column order matters*

```sql
WITH u AS ( SELECT oi.product_id, SUM(oi.quantity) AS units FROM order_items oi JOIN orders o ON o.order_id = oi.order_id WHERE o.status = 'completed' GROUP BY oi.product_id), r AS (SELECT product_id, units, RANK() OVER (ORDER BY units DESC) AS rnk FROM u) SELECT product_id, units, rnk FROM r WHERE rnk <= 10
```

First rows: (24, 485, 1); (40, 480, 2); (3, 441, 3); ...

### ranking-2 (medium, window, join)

Within each country, rank customers by their total revenue using dense ranking, highest revenue first, where customers with equal revenue share a rank and the next rank is not skipped (1, 2, 2, 3). Return the country code, customer id, the customer's revenue and the rank for every customer ranked 1, 2 or 3 in their country. Revenue means quantity times unit price added up over order lines, counting completed orders only. Customers with no completed orders are ignored, and the country is the one on the customer record.

*18 rows x 4 columns; column order matters; example (may appear in tests)*

```sql
WITH rev AS ( SELECT c.country, c.customer_id, SUM(oi.quantity * oi.unit_price) AS revenue FROM customers c JOIN orders o ON o.customer_id = c.customer_id JOIN order_items oi ON oi.order_id = o.order_id WHERE o.status = 'completed' GROUP BY c.country, c.customer_id), r AS (SELECT *, DENSE_RANK() OVER (PARTITION BY country ORDER BY revenue DESC) AS rnk FROM rev) SELECT country, customer_id, revenue, rnk FROM r WHERE rnk <= 3
```

First rows: (SG, 189, 85740.08, 1); (SG, 318, 27618.94, 2); (SG, 24, 21400.45, 3); ...

### ranking-3 (hard, window, cte)

Rank all products by units sold on completed orders (quantity added up over order lines) separately for calendar 2024 and for calendar 2025, using the order timestamp's year, most units first, where products with equal units share the same rank and the next rank is skipped (1, 2, 2, 4). Products with no completed sales in a year are not ranked for that year. Return the product id, its 2024 rank and its 2025 rank for every product that is ranked in both years and whose 2025 rank is at least 10 places better (numerically lower) than its 2024 rank.

*18 rows x 3 columns; column order matters*

```sql
WITH u AS ( SELECT oi.product_id, year(o.order_ts) AS yr, SUM(oi.quantity) AS units FROM order_items oi JOIN orders o ON o.order_id = oi.order_id WHERE o.status = 'completed' AND year(o.order_ts) IN (2024, 2025) GROUP BY oi.product_id, year(o.order_ts)), r AS (SELECT product_id, yr, RANK() OVER (PARTITION BY yr ORDER BY units DESC) AS rnk FROM u), w AS ( SELECT product_id, MAX(CASE WHEN yr = 2024 THEN rnk END) AS rank_2024, MAX(CASE WHEN yr = 2025 THEN rnk END) AS rank_2025 FROM r GROUP BY product_id) SELECT product_id, rank_2024, rank_2025 FROM w WHERE rank_2024 IS NOT NULL AND rank_2025 IS NOT NULL AND rank_2024 - rank_2025 >= 10
```

First rows: (1, 26, 11); (3, 23, 1); (4, 45, 14); ...

### top_n_per_group-1 (easy, window, join)

For each product category, which product earned the most revenue? Return the category, the product id and that product's revenue. Revenue means quantity times unit price added up over order lines, counting completed orders only. If two products in a category tie on revenue, return the one with the lower product id.

*8 rows x 3 columns; column order matters*

```sql
WITH p AS ( SELECT pr.category, pr.product_id, SUM(oi.quantity * oi.unit_price) AS revenue FROM order_items oi JOIN orders o ON o.order_id = oi.order_id JOIN products pr ON pr.product_id = oi.product_id WHERE o.status = 'completed' GROUP BY pr.category, pr.product_id) SELECT category, product_id, revenue FROM p QUALIFY ROW_NUMBER() OVER (PARTITION BY category ORDER BY revenue DESC, product_id) = 1
```

First rows: (Wearables, 45, 142502.56); (Home, 28, 168315.45); (Gaming, 20, 145719.97); ...

### top_n_per_group-2 (medium, window, join)

For each product category, find the two customers who spent the most on products in that category. Return the category, the customer id and that customer's spend in the category. Spend is quantity times unit price added up over order lines of that category, counting completed orders only. If customers tie, the lower customer id comes first.

*16 rows x 3 columns; column order matters*

```sql
WITH s AS ( SELECT pr.category, o.customer_id, SUM(oi.quantity * oi.unit_price) AS spend FROM order_items oi JOIN orders o ON o.order_id = oi.order_id JOIN products pr ON pr.product_id = oi.product_id WHERE o.status = 'completed' GROUP BY pr.category, o.customer_id) SELECT category, customer_id, spend FROM s QUALIFY ROW_NUMBER() OVER (PARTITION BY category ORDER BY spend DESC, customer_id) <= 2
```

First rows: (Home, 169, 21394.53); (Home, 318, 8062.33); (Wearables, 169, 19207.85); ...

### top_n_per_group-3 (hard, window, cte)

Take the five customers with the highest total revenue, and for each of them list their three largest completed orders. Return the customer id, the order id and the order's value (quantity times unit price added up over its lines). Revenue means quantity times unit price added up over order lines, counting completed orders only. If customers tie on total revenue, the lower customer id wins; if a customer's orders tie on value, the lower order id comes first.

*15 rows x 3 columns; column order matters*

```sql
WITH ov AS ( SELECT o.order_id, o.customer_id, SUM(oi.quantity * oi.unit_price) AS order_value FROM orders o JOIN order_items oi ON oi.order_id = o.order_id WHERE o.status = 'completed' GROUP BY o.order_id, o.customer_id), top5 AS (SELECT customer_id FROM ov GROUP BY customer_id ORDER BY SUM(order_value) DESC, customer_id LIMIT 5) SELECT ov.customer_id, ov.order_id, ov.order_value FROM ov JOIN top5 t ON t.customer_id = ov.customer_id QUALIFY ROW_NUMBER() OVER (PARTITION BY ov.customer_id ORDER BY ov.order_value DESC, ov.order_id) <= 3
```

First rows: (247, 264, 4373.22); (247, 2571, 3469.37); (247, 587, 3410.98); ...

### recursive_hierarchy-1 (easy, recursive)

List the employee ids of everyone who reports to employee 2, directly or through other managers, not including employee 2 themselves.

*9 rows x 1 columns; example (may appear in tests)*

```sql
WITH RECURSIVE below AS ( SELECT employee_id FROM employees WHERE manager_id = 2 UNION ALL SELECT e.employee_id FROM employees e JOIN below b ON e.manager_id = b.employee_id) SELECT employee_id FROM below
```

First rows: (6); (10); (14); ...

### recursive_hierarchy-2 (medium, recursive, join)

For every employee whose title is Analyst, return the analyst's employee id and the employee id of the VP at the top of their reporting chain (the VP who sits directly under the CEO).

*20 rows x 2 columns; column order matters*

```sql
WITH RECURSIVE org AS ( SELECT employee_id, employee_id AS vp_id FROM employees WHERE title = 'VP' UNION ALL SELECT e.employee_id, o.vp_id FROM employees e JOIN org o ON e.manager_id = o.employee_id) SELECT o.employee_id AS analyst_id, o.vp_id FROM org o JOIN employees e ON e.employee_id = o.employee_id WHERE e.title = 'Analyst'
```

First rows: (34, 2); (35, 3); (32, 4); ...

### recursive_hierarchy-3 (hard, recursive, window)

For each VP, find the person in that VP's organisation (everyone who reports to them directly or through other managers, not counting the VP) who was hired earliest. Return the VP's employee id, that person's employee id and their hire date. If two people share the earliest hire date, take the lower employee id.

*4 rows x 3 columns; column order matters*

```sql
WITH RECURSIVE org AS ( SELECT employee_id AS vp_id, employee_id FROM employees WHERE title = 'VP' UNION ALL SELECT o.vp_id, e.employee_id FROM employees e JOIN org o ON e.manager_id = o.employee_id), members AS (SELECT o.vp_id, o.employee_id, e.hire_date FROM org o JOIN employees e ON e.employee_id = o.employee_id WHERE o.employee_id <> o.vp_id) SELECT vp_id, employee_id, hire_date FROM members QUALIFY ROW_NUMBER() OVER (PARTITION BY vp_id ORDER BY hire_date, employee_id) = 1
```

First rows: (2, 24, 2024-01-09); (4, 32, 2024-07-18); (5, 23, 2024-04-09); ...

### date_spine-1 (easy, recursive, join)

How many completed orders were placed on each day from 1 March 2024 to 14 March 2024 inclusive? Return every one of those 14 days (as a date) with its number of completed orders, using 0 for days with none, in date order. An order's day is the date of its order timestamp.

*14 rows x 2 columns; row order matters; column order matters*

```sql
WITH RECURSIVE spine AS ( SELECT DATE '2024-03-01' AS day UNION ALL SELECT day + 1 FROM spine WHERE day < DATE '2024-03-14'), daily AS ( SELECT CAST(order_ts AS DATE) AS day, COUNT(*) AS orders FROM orders WHERE status = 'completed' AND order_ts >= DATE '2024-03-01' AND order_ts < DATE '2024-03-15' GROUP BY 1) SELECT s.day, COALESCE(d.orders, 0) AS orders FROM spine s LEFT JOIN daily d ON d.day = s.day ORDER BY s.day
```

First rows: (2024-03-01, 0); (2024-03-02, 1); (2024-03-03, 1); ...

### date_spine-2 (medium, recursive)

List every date in February 2024 on which no completed order was placed, in date order. An order's day is the date of its order timestamp.

*24 rows x 1 columns; row order matters*

```sql
WITH RECURSIVE spine AS ( SELECT DATE '2024-02-01' AS day UNION ALL SELECT day + 1 FROM spine WHERE day < DATE '2024-02-29') SELECT s.day FROM spine s WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.status = 'completed' AND CAST(o.order_ts AS DATE) = s.day) ORDER BY s.day
```

First rows: (2024-02-01); (2024-02-02); (2024-02-03); ...

### date_spine-3 (hard, recursive, window, join)

For each day of May 2026, show the date and the average daily revenue over that day and the six days before it (a seven-day trailing average), rounded to 2 decimals. Days with no completed orders count as zero revenue, and the days before 1 May come from late April. Revenue means quantity times unit price added up over order lines, counting completed orders only. An order's day is the date of its order timestamp. List in date order.

*31 rows x 2 columns; row order matters; column order matters; example (may appear in tests)*

```sql
WITH RECURSIVE spine AS ( SELECT DATE '2026-04-25' AS day UNION ALL SELECT day + 1 FROM spine WHERE day < DATE '2026-05-31'), daily AS ( SELECT CAST(o.order_ts AS DATE) AS day, SUM(oi.quantity * oi.unit_price) AS revenue FROM orders o JOIN order_items oi ON oi.order_id = o.order_id WHERE o.status = 'completed' AND o.order_ts >= DATE '2026-04-25' AND o.order_ts < DATE '2026-06-01' GROUP BY 1), f AS (SELECT s.day, COALESCE(d.revenue, 0) AS revenue FROM spine s LEFT JOIN daily d ON d.day = s.day), w AS (SELECT day, ROUND(AVG(revenue) OVER (ORDER BY day ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 2) AS trailing_7d_avg FROM f) SELECT day, trailing_7d_avg FROM w WHERE day >= DATE '2026-05-01' ORDER BY day
```

First rows: (2026-05-01, 6786.87); (2026-05-02, 6087.42); (2026-05-03, 6501.33); ...

### gaps_and_islands-1 (easy, window)

Customer 281 browsed the site on several days. List each run of two or more consecutive calendar days on which they viewed at least one page: the first day of the run, the last day of the run, and the number of days in it. Order by first day.

*2 rows x 3 columns; row order matters; column order matters*

```sql
WITH d AS (SELECT DISTINCT CAST(event_ts AS DATE) AS day FROM page_events WHERE customer_id = 281), k AS (SELECT day, day - CAST(ROW_NUMBER() OVER (ORDER BY day) AS INTEGER) AS grp FROM d) SELECT MIN(day) AS first_day, MAX(day) AS last_day, COUNT(*) AS days FROM k GROUP BY grp HAVING COUNT(*) >= 2 ORDER BY first_day
```

First rows: (2024-02-15, 2024-02-16, 2); (2025-01-27, 2025-01-28, 2)

### gaps_and_islands-2 (medium, window, cte)

Which customers have viewed pages on seven or more consecutive calendar days at some point? Return each such customer's id and the length in days of their longest run of consecutive days with at least one page view.

*55 rows x 2 columns; column order matters*

```sql
WITH d AS (SELECT DISTINCT customer_id, CAST(event_ts AS DATE) AS day FROM page_events), k AS (SELECT customer_id, day, day - CAST(ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY day) AS INTEGER) AS grp FROM d), r AS (SELECT customer_id, COUNT(*) AS run_days FROM k GROUP BY customer_id, grp) SELECT customer_id, MAX(run_days) AS longest_run_days FROM r GROUP BY customer_id HAVING MAX(run_days) >= 7
```

First rows: (3, 10); (16, 9); (50, 7); ...

### gaps_and_islands-3 (hard, window, cte)

For each customer, find their longest run of consecutive calendar months in which they placed at least one completed order. Return only customers whose longest run is 4 months or more. Return the customer id, the first month of that run and the last month of that run (both as text in YYYY-MM form), and the length of the run in months. If a customer has two runs of the same longest length, use the earlier one. A month is the calendar month of the order timestamp.

*26 rows x 4 columns; column order matters*

```sql
WITH m AS ( SELECT DISTINCT customer_id, year(order_ts) * 12 + month(order_ts) AS mi FROM orders WHERE status = 'completed'), k AS (SELECT customer_id, mi, mi - ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY mi) AS grp FROM m), r AS (SELECT customer_id, MIN(mi) AS first_mi, MAX(mi) AS last_mi, COUNT(*) AS run_months FROM k GROUP BY customer_id, grp), b AS (SELECT * FROM r QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY run_months DESC, first_mi) = 1) SELECT customer_id, printf('%04d-%02d', (first_mi - 1) // 12, (first_mi - 1) % 12 + 1) AS first_month, printf('%04d-%02d', (last_mi - 1) // 12, (last_mi - 1) % 12 + 1) AS last_month, run_months FROM b WHERE run_months >= 4
```

First rows: (146, 2025-02, 2025-05, 4); (394, 2026-01, 2026-06, 6); (33, 2025-10, 2026-01, 4); ...

### subscription_lapses-1 (easy, window)

List every lapse in customer 46's subscription history: the date the earlier coverage ended, the date the subscription resumed, and the length of the lapse in days. A subscription period covers the dates from its start date to its end date. Periods of one customer can overlap or abut; a lapse is a gap where a period starts after the latest end date of all of that customer's earlier periods (ordered by start date, then end date). The lapse runs from that latest earlier end date to the new period's start date, and its length in days is the difference between those two dates. Order by the date coverage ended.

*3 rows x 3 columns; row order matters; column order matters*

```sql
WITH b AS ( SELECT period_start, period_end, MAX(period_end) OVER (ORDER BY period_start, period_end ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_max_end FROM subscriptions WHERE customer_id = 46) SELECT prior_max_end AS coverage_ended, period_start AS resumed_on, date_diff('day', prior_max_end, period_start) AS lapse_days FROM b WHERE prior_max_end IS NOT NULL AND period_start > prior_max_end ORDER BY prior_max_end
```

First rows: (2024-12-30, 2025-02-03, 35); (2025-03-05, 2025-04-10, 36); (2025-06-07, 2025-08-08, 62)

### subscription_lapses-2 (medium, window, cte)

What are the ten longest subscription lapses across all customers? Return the customer id, the date the earlier coverage ended, the date the subscription resumed, and the lapse length in days, longest first. A subscription period covers the dates from its start date to its end date. Periods of one customer can overlap or abut; a lapse is a gap where a period starts after the latest end date of all of that customer's earlier periods (ordered by start date, then end date). The lapse runs from that latest earlier end date to the new period's start date, and its length in days is the difference between those two dates. If lapses tie on length, order by customer id, then by the date coverage ended.

*10 rows x 4 columns; row order matters; column order matters*

```sql
WITH b AS ( SELECT customer_id, period_start, period_end, MAX(period_end) OVER (PARTITION BY customer_id ORDER BY period_start, period_end ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_max_end FROM subscriptions) SELECT customer_id, prior_max_end AS coverage_ended, period_start AS resumed_on, date_diff('day', prior_max_end, period_start) AS lapse_days FROM b WHERE prior_max_end IS NOT NULL AND period_start > prior_max_end ORDER BY lapse_days DESC, customer_id, coverage_ended LIMIT 10
```

First rows: (84, 2025-10-08, 2026-01-05, 89); (95, 2024-07-06, 2024-10-01, 87); (78, 2024-07-01, 2024-09-24, 85); ...

### subscription_lapses-3 (hard, window, cte)

Which five customers have the most total days of subscription coverage? Merge each customer's periods into continuous stretches: a period joins the current stretch when it starts on or before the latest end date of the earlier periods, otherwise it begins a new stretch (periods ordered by start date, then end date). A stretch's length in days is its last end date minus its first start date, and a customer's total is the sum over their stretches, so overlapping time is counted once. Return the customer id and the total days, most days first; ties go to the lower customer id.

*5 rows x 2 columns; row order matters; column order matters*

```sql
WITH b AS ( SELECT customer_id, period_start, period_end, MAX(period_end) OVER (PARTITION BY customer_id ORDER BY period_start, period_end ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_max_end FROM subscriptions), f AS (SELECT *, CASE WHEN prior_max_end IS NULL OR period_start > prior_max_end THEN 1 ELSE 0 END AS new_stretch FROM b), s AS (SELECT *, SUM(new_stretch) OVER (PARTITION BY customer_id ORDER BY period_start, period_end ROWS UNBOUNDED PRECEDING) AS stretch_id FROM f), st AS (SELECT customer_id, stretch_id, date_diff('day', MIN(period_start), MAX(period_end)) AS days FROM s GROUP BY customer_id, stretch_id) SELECT customer_id, SUM(days) AS total_days FROM st GROUP BY customer_id ORDER BY total_days DESC, customer_id LIMIT 5
```

First rows: (94, 360); (148, 358); (4, 357); ...

### sessionization-1 (easy, window)

Using a 10-minute session rule, how many browsing sessions and how many page views did each of customers 3, 26 and 127 have? Return the customer id, the number of sessions and the number of page views. A customer's page views are ordered by event timestamp (then event id); a new session starts at a customer's first page view, and whenever more than 10 minutes have passed since that customer's previous page view. A gap of exactly 10 minutes stays in the same session.

*3 rows x 3 columns; column order matters*

```sql
WITH f AS ( SELECT customer_id, CASE WHEN LAG(event_ts) OVER w IS NULL OR event_ts - LAG(event_ts) OVER w > INTERVAL 10 MINUTE THEN 1 ELSE 0 END AS new_session FROM page_events WHERE customer_id IN (3, 26, 127) WINDOW w AS (PARTITION BY customer_id ORDER BY event_ts, event_id)) SELECT customer_id, SUM(new_session) AS sessions, COUNT(*) AS page_views FROM f GROUP BY customer_id
```

First rows: (3, 35, 67); (26, 17, 29); (127, 16, 29)

### sessionization-2 (medium, window)

Using a 10-minute session rule across all customers, how many browsing sessions are there in total, and how many page views does a session contain on average (total page views divided by total sessions, rounded to 2 decimals)? Return the two numbers in that order. A customer's page views are ordered by event timestamp (then event id); a new session starts at a customer's first page view, and whenever more than 10 minutes have passed since that customer's previous page view. A gap of exactly 10 minutes stays in the same session.

*1 rows x 2 columns; column order matters*

```sql
WITH f AS ( SELECT CASE WHEN LAG(event_ts) OVER w IS NULL OR event_ts - LAG(event_ts) OVER w > INTERVAL 10 MINUTE THEN 1 ELSE 0 END AS new_session FROM page_events WINDOW w AS (PARTITION BY customer_id ORDER BY event_ts, event_id)) SELECT SUM(new_session) AS sessions, ROUND(CAST(COUNT(*) AS DOUBLE) / SUM(new_session), 2) AS avg_page_views FROM f
```

First rows: (6923, 1.78)

### sessionization-3 (hard, window, cte)

Use a stricter 15-minute rule: a customer's page views are ordered by event timestamp (then event id), and a new session starts at the customer's first page view and whenever more than 15 minutes have passed since their previous page view (a gap of exactly 15 minutes stays in the same session). Which ten customers had the most sessions under this rule? Return the customer id and the number of sessions, most sessions first; ties go to the lower customer id.

*10 rows x 2 columns; row order matters; column order matters*

```sql
WITH f AS ( SELECT customer_id, CASE WHEN LAG(event_ts) OVER w IS NULL OR event_ts - LAG(event_ts) OVER w > INTERVAL 15 MINUTE THEN 1 ELSE 0 END AS new_session FROM page_events WINDOW w AS (PARTITION BY customer_id ORDER BY event_ts, event_id)) SELECT customer_id, SUM(new_session) AS sessions FROM f GROUP BY customer_id ORDER BY sessions DESC, customer_id LIMIT 10
```

First rows: (109, 36); (202, 31); (399, 28); ...

### pivot-1 (easy, pivot, join)

Show the number of completed orders for each country (rows) split by customer segment (columns). Return one row per country with columns for the country code and the counts for consumer, smb and enterprise customers, using 0 where there are none. A customer's country and segment come from the customer record.

*6 rows x 4 columns; column order matters; example (may appear in tests)*

```sql
SELECT c.country, COUNT(*) FILTER (WHERE c.segment = 'consumer') AS consumer, COUNT(*) FILTER (WHERE c.segment = 'smb') AS smb, COUNT(*) FILTER (WHERE c.segment = 'enterprise') AS enterprise FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE o.status = 'completed' GROUP BY c.country
```

First rows: (AU, 29, 39, 24); (GB, 218, 87, 41); (SG, 178, 39, 7); ...

### pivot-2 (medium, pivot, join)

Show 2025 revenue by month (rows) and product category (columns). Return one row per month of 2025, with the month as text in YYYY-MM form followed by one revenue column for each of the eight categories (Audio, Cameras, Computing, Gaming, Home, Mobile, Networking, Wearables), using 0 where a category had no revenue that month. Revenue means quantity times unit price added up over order lines, counting completed orders only. An order belongs to the month of its order timestamp; a line belongs to the category of its product.

*12 rows x 9 columns; column order matters*

```sql
WITH monthly AS ( SELECT strftime(o.order_ts, '%Y-%m') AS month, p.category, SUM(oi.quantity * oi.unit_price) AS revenue FROM orders o JOIN order_items oi ON oi.order_id = o.order_id JOIN products p ON p.product_id = oi.product_id WHERE o.status = 'completed' AND o.order_ts >= DATE '2025-01-01' AND o.order_ts < DATE '2026-01-01' GROUP BY 1, 2) SELECT month, COALESCE("Audio", 0) AS audio, COALESCE("Cameras", 0) AS cameras, COALESCE("Computing", 0) AS computing, COALESCE("Gaming", 0) AS gaming, COALESCE("Home", 0) AS home, COALESCE("Mobile", 0) AS mobile, COALESCE("Networking", 0) AS networking, COALESCE("Wearables", 0) AS wearables FROM (PIVOT monthly ON category IN ('Audio','Cameras','Computing','Gaming','Home','Mobile','Networking','Wearables') USING SUM(revenue) GROUP BY month)
```

First rows: (2025-02, 15784.18, 18345.28, 17625.59, 18198.37, 15455.88, 13001.74, 24239.04, 15214.70); (2025-03, 16538.56, 22834.09, 21954.52, 18244.34, 20891.53, 29927.75, 20749.54, 17344.13); (2025-07, 19047.64, 26557.87, 23731.36, 22876.41, 19404.58, 25369.35, 27792.08, 17153.95); ...

### pivot-3 (hard, pivot, join)

For each product category, show completed revenue for calendar 2024, calendar 2025 and calendar 2026 (to date) side by side, plus the growth of 2025 over 2024 as a percentage rounded to 1 decimal ((2025 minus 2024) divided by 2024, times 100). Return the category and four more columns in that order: revenue 2024, revenue 2025, revenue 2026, growth percentage. Revenue means quantity times unit price added up over order lines, counting completed orders only. An order belongs to the year of its order timestamp.

*8 rows x 5 columns; column order matters*

```sql
WITH y AS ( SELECT p.category, year(o.order_ts) AS yr, SUM(oi.quantity * oi.unit_price) AS revenue FROM orders o JOIN order_items oi ON oi.order_id = o.order_id JOIN products p ON p.product_id = oi.product_id WHERE o.status = 'completed' GROUP BY 1, 2), w AS ( SELECT category, SUM(CASE WHEN yr = 2024 THEN revenue ELSE 0 END) AS rev_2024, SUM(CASE WHEN yr = 2025 THEN revenue ELSE 0 END) AS rev_2025, SUM(CASE WHEN yr = 2026 THEN revenue ELSE 0 END) AS rev_2026 FROM y GROUP BY category) SELECT category, rev_2024, rev_2025, rev_2026, ROUND(100.0 * (rev_2025 - rev_2024) / rev_2024, 1) AS growth_pct FROM w
```

First rows: (Gaming, 79201.13, 244187.52, 156447.57, 208.3); (Wearables, 101966.77, 234803.04, 135035.57, 130.3); (Home, 111864.04, 287495.41, 164995.46, 157.0); ...

### conditional_aggregation-1 (easy, aggregation, join)

For each customer segment, return the segment, the total number of orders placed (all statuses), the number of those that were cancelled, and the cancellation rate as a percentage of all the segment's orders rounded to 1 decimal. A customer's segment comes from the customer record.

*3 rows x 4 columns; column order matters*

```sql
SELECT c.segment, COUNT(*) AS total_orders, SUM(CASE WHEN o.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_orders, ROUND(100.0 * SUM(CASE WHEN o.status = 'cancelled' THEN 1 ELSE 0 END) / COUNT(*), 1) AS cancellation_rate_pct FROM orders o JOIN customers c ON c.customer_id = o.customer_id GROUP BY c.segment
```

First rows: (enterprise, 276, 32, 11.6); (consumer, 1961, 200, 10.2); (smb, 763, 91, 11.9)

### conditional_aggregation-2 (medium, aggregation, join)

For each product category, return the category, the revenue from completed orders, the value of returned orders (the same quantity times unit price measure, but over orders whose status is returned), and the return rate: returned value divided by the sum of completed revenue and returned value, as a percentage rounded to 2 decimals. Cancelled orders are ignored. A line belongs to the category of its product.

*8 rows x 4 columns; column order matters; example (may appear in tests)*

```sql
WITH t AS ( SELECT p.category, SUM(CASE WHEN o.status = 'completed' THEN oi.quantity * oi.unit_price ELSE 0 END) AS completed_revenue, SUM(CASE WHEN o.status = 'returned' THEN oi.quantity * oi.unit_price ELSE 0 END) AS returned_value FROM orders o JOIN order_items oi ON oi.order_id = o.order_id JOIN products p ON p.product_id = oi.product_id GROUP BY p.category) SELECT category, completed_revenue, returned_value, ROUND(100.0 * returned_value / (completed_revenue + returned_value), 2) AS return_rate_pct FROM t
```

First rows: (Home, 564354.91, 47637.71, 7.78); (Mobile, 554223.18, 42508.05, 7.12); (Audio, 415247.65, 30633.90, 6.87); ...

### conditional_aggregation-3 (hard, aggregation, cte)

For each country, count customers by how many completed orders they have placed: one-time customers (exactly 1 completed order), repeat customers (2 to 4) and loyal customers (5 or more). Return the country code followed by those three counts, in that order. Customers with no completed orders are not counted anywhere. A customer's country comes from the customer record.

*6 rows x 4 columns; column order matters*

```sql
WITH n AS (SELECT customer_id, COUNT(*) AS n FROM orders WHERE status = 'completed' GROUP BY customer_id) SELECT c.country, SUM(CASE WHEN n.n = 1 THEN 1 ELSE 0 END) AS one_time, SUM(CASE WHEN n.n BETWEEN 2 AND 4 THEN 1 ELSE 0 END) AS repeat_customers, SUM(CASE WHEN n.n >= 5 THEN 1 ELSE 0 END) AS loyal FROM n JOIN customers c ON c.customer_id = n.customer_id GROUP BY c.country
```

First rows: (AU, 2, 8, 9); (GB, 1, 25, 33); (US, 3, 42, 58); ...

### tie_safe_ranking-1 (easy, window)

For each of customers 1 to 15, which page did they view first? Return the customer id and the page. Page views are ordered by event timestamp; when several of a customer's page views share the exact same timestamp, the lower event id comes first.

*15 rows x 2 columns; column order matters*

```sql
SELECT customer_id, page FROM page_events WHERE customer_id BETWEEN 1 AND 15 QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY event_ts, event_id) = 1
```

First rows: (4, /); (1, /); (12, /support); ...

### tie_safe_ranking-2 (medium, window)

For each customer and each calendar day on which they viewed pages, take their last page view of that day (the latest event timestamp; when several share it, the one with the higher event id). Then, for each page, count how many of those customer-days ended on that page. Return the page and the number of customer-days.

*8 rows x 2 columns; column order matters*

```sql
WITH l AS ( SELECT page FROM page_events QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id, CAST(event_ts AS DATE) ORDER BY event_ts DESC, event_id DESC) = 1) SELECT page, COUNT(*) AS customer_days FROM l GROUP BY page
```

First rows: (/account, 280); (/, 264); (/cart, 288); ...

### tie_safe_ranking-3 (hard, window)

For every customer with at least three page views, take their third page view (counting from their earliest). Then, for each page, count how many customers' third page view was on that page. Return the page and the number of customers. Page views are ordered by event timestamp; when several of a customer's page views share the exact same timestamp, the lower event id comes first.

*8 rows x 2 columns; column order matters*

```sql
WITH t AS ( SELECT page FROM page_events QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY event_ts, event_id) = 3) SELECT page, COUNT(*) AS customers FROM t GROUP BY page
```

First rows: (/search, 41); (/category, 48); (/account, 50); ...

