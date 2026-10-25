# Golden set authoring notes

> **Reviewed by the owner 2026-10-25: structure approved, sessionization-1/-2 revised. FROZEN
> 2026-10-25** after the owner confirmed `PRE_FREEZE_REVIEW.md`.
>
> `questions.jsonl` carries an `expected_digest` per question, frozen against the seeded
> warehouse (seed 20260830: 400 customers, 48 products, 35 employees, 3,000 orders, 9,001 order
> lines, 12,301 page events, 530 subscription periods). Any later edit to a question, a reference
> query or the equality rule invalidates a real run.

36 questions: 12 SQL patterns x 3 (qids `<pattern>-1` easy, `-2` medium, `-3` hard). Six rows are
marked `"example": true` (one per pattern, for six different patterns) and may appear in test
fixtures; the other 30 stay held out.

| Example qid | Difficulty |
|---|---|
| running_total-1 | easy |
| ranking-2 | medium |
| recursive_hierarchy-1 | easy |
| date_spine-3 | hard |
| pivot-1 | easy |
| conditional_aggregation-2 | medium |

## Business definitions used across the questions

Every definition that matters is also written into the question text, so a reader needs nothing
beyond the question and the schema.

| Term | Definition used |
|---|---|
| Revenue / order value / spend | `SUM(quantity * unit_price)` over `order_items`, joined to `orders`, **completed orders only** (`status = 'completed'`). `unit_price` already includes any discount; `products.list_price` is never used. |
| Returned value | The same measure over orders with `status = 'returned'` (only conditional_aggregation-2). |
| Orders placed | All statuses (only conditional_aggregation-1 and pivot-1's "completed orders" count differ; each says which). |
| Day / month / quarter / year of an order | Calendar part of `order_ts` (a timestamp with no time zone). Quarter text looks like `2025-Q3`, month text like `2025-03`. |
| Customer country / segment | The values on the `customers` row. |
| Event ordering | `event_ts`, then `event_id`. The warehouse has timestamps shared by several events of one customer, so the question text states the tie rule wherever it matters. |
| Session | A new session starts at a customer's first page view and whenever the gap to the previous page view is **strictly more than** the threshold. A gap of exactly the threshold stays in the session. Threshold is 10 minutes (sessionization-1, -2) or 15 minutes (sessionization-3). |
| Subscription coverage | A period covers the dates from `period_start` to `period_end`. A **lapse** is a gap where a period starts after the latest end date of all earlier periods of that customer; its length is `period_start - that end date` in days. A merged stretch's length is `last end - first start`. "Active subscription on a date" is not used by any question. |
| Streak | Consecutive calendar days (page views) or consecutive calendar months (completed orders). |
| Rounding | Stated in each question that rounds. All rounded reference values were checked to sit well away from a half-way boundary, so half-up and half-even agree. |
| Ranking ties | Always stated: competition rank (1, 2, 2, 4) or dense rank (1, 2, 2, 3); top-N tie-breakers are an explicit lower id. |

Conventions: no `now()` or `current_date` anywhere; windows use explicit `ROWS` frames; every
ranking or top-N has a total tie-breaker; `order_matters` is `true` only where the question asks
for an order and the reference has a total `ORDER BY`.

## Per-question table

Rows and columns are what the reference returns on the seeded warehouse. "Recipe" is the file in
`reference/recipes/` whose pattern the question adapts to the full warehouse.

| qid | Recipe | Rows x cols | Ambiguity resolved, and how |
|---|---|---|---|
| running_total-1 | 01 | 12 x 3 | Month text format and the revenue definition are stated. Ordered. |
| running_total-2 | 01 | 24 x 4 | Country from the customer record; quarter text format given; every country has revenue in every 2025 quarter, so no missing-row ambiguity. Ordered by country, quarter. |
| running_total-3 | 01 | 5 x 3 | "First took the cumulative spend to 50,000 or more"; order sequence is `order_ts` then `order_id`. Only five heavy-tail customers reach 50,000. |
| lag_lead-1 | 02 | 7 x 3 | Customer 13 chosen for 7 completed orders. First row's gap is empty (NULL). Day difference is between calendar dates. Ordered. |
| lag_lead-2 | 02 | 3 x 2 | Average is over all gaps in the segment (not an average of per-customer averages). Rounded to 2 decimals. |
| lag_lead-3 | 02 | 6 x 2 | "Followed" = next completed order's date is at most 30 days later; a customer's last order is not followed. Percentage is over all the country's completed orders; rounded to 1 decimal. |
| ranking-1 | 03 | 10 x 3 | Competition rank, ties share a rank and the next is skipped. Rank 7 is a two-way tie, so 10 rows are returned for "ranked 10th or better". |
| ranking-2 | 03 | 18 x 4 | Dense rank per country; no revenue ties among the top 3 in this data (18 rows = 6 countries x 3). |
| ranking-3 | 03 | 18 x 3 | Year of `order_ts`; competition rank within each year; "at least 10 places better" means `rank_2024 - rank_2025 >= 10`. |
| top_n_per_group-1 | 04 | 8 x 3 | One product per category; tie rule stated (no tie occurs). |
| top_n_per_group-2 | 04 | 16 x 3 | Spend in a category counts only lines of that category's products. Tie rule stated. |
| top_n_per_group-3 | 04 | 15 x 3 | Nested: top five customers by total revenue, then each one's three largest orders. Tie rules for both levels stated. |
| recursive_hierarchy-1 | 05 | 9 x 1 | Employee 2 is a VP; result is the managers and analysts under them, excluding employee 2. |
| recursive_hierarchy-2 | 05 | 20 x 2 | "VP at the top of the chain" = the VP directly under the CEO. |
| recursive_hierarchy-3 | 05 | 4 x 3 | Earliest `hire_date` within the VP's organisation, VP excluded; lower id on ties (no tie occurs). |
| date_spine-1 | 06 | 14 x 2 | 1-14 March 2024 chosen because early-2024 volume leaves 8 zero days (a 2026 window had none, which would not test the spine). Day returned as a date. Ordered. |
| date_spine-2 | 06 | 24 x 1 | February 2024 is a leap month (29 days); a day with any completed order is excluded. Ordered. |
| date_spine-3 | 06 | 31 x 2 | Trailing window is the day plus six earlier days, zero-filled; the first days of May reach back into late April. Rounded to 2 decimals. Ordered. |
| gaps_and_islands-1 | 07 | 2 x 3 | Customer 281 has two 2-day runs. Runs of one day are excluded. Ordered. |
| gaps_and_islands-2 | 07 | 55 x 2 | A run is consecutive days with at least one page view (several views a day count once). Threshold 7 days. |
| gaps_and_islands-3 | 07 | 26 x 4 | Month runs are over completed orders; ties between equal-length longest runs go to the earlier run; month text format given. Threshold 4 months. |
| subscription_lapses-1 | 08 | 3 x 3 | Customer 46 has three lapses. The full lapse definition is in the text. Ordered. |
| subscription_lapses-2 | 08 | 10 x 4 | Ten longest lapses; ties by customer id then date (the 10th lapse is 75 days, the 11th 74, so no tie at the cut). Ordered. |
| subscription_lapses-3 | 08 | 5 x 2 | Overlap counted once; abutting periods join one stretch; days = last end minus first start. Fifth is 351, sixth 326, so no tie at the cut. Ordered. |
| sessionization-1 | 09 | 3 x 3 | **Revised 2026-10-25 (owner review).** 10-minute rule, customers 3, 26 and 127: 35 / 17 / 16 sessions over 67 / 29 / 29 views. Each of the three has one gap of exactly 10 minutes and many gaps either side, so each row changes (36 / 18 / 17) under "10 minutes or more". Not ordered. |
| sessionization-2 | 09 | 1 x 2 | **Revised 2026-10-25 (owner review).** 10-minute rule over all customers: 6,923 sessions, 1.78 views per session (raw 1.7768). The data has 5,370 gaps under 10 minutes, 8 exactly at 10 minutes (8 different customers) and 6,523 over, so "10 minutes or more" gives 6,931 and 1.77. |
| sessionization-3 | 09 | 10 x 2 | 15-minute rule. In this warehouse views inside a visit are up to 20 minutes apart, so a 15-minute rule splits visits and is the question where the threshold actually changes the answer. Five customers tie at 24 sessions and all five are inside the top ten; the 11th has 23. Ordered. |
| pivot-1 | 10 | 6 x 4 | Counts of completed orders; 0 where none (none occurs). Does not require PIVOT syntax. |
| pivot-2 | 10 | 12 x 9 | Eight category columns named in the question; no empty cells occur in 2025. Reference uses `PIVOT`; a `SUM(CASE ...)` form gives the same values. |
| pivot-3 | 10 | 8 x 5 | Column order given in the text; growth rounded to 1 decimal. |
| conditional_aggregation-1 | 11 | 3 x 4 | All statuses counted as "orders placed"; rate rounded to 1 decimal. |
| conditional_aggregation-2 | 11 | 8 x 4 | Cancelled orders excluded from the rate; rate is returned / (completed + returned); rounded to 2 decimals. |
| conditional_aggregation-3 | 11 | 6 x 4 | Buckets 1, 2-4, 5+ completed orders; customers with none excluded. |
| tie_safe_ranking-1 | 12 | 15 x 2 | Customers 1-15; first view by `event_ts` then lower `event_id`. |
| tie_safe_ranking-2 | 12 | 8 x 2 | Last view of each customer-day by `event_ts` then higher `event_id`; counted per page. |
| tie_safe_ranking-3 | 12 | 8 x 2 | Third view overall (by `event_ts`, then `event_id`); customers with fewer than three views are excluded. |

## How the references were checked

- Every reference runs on a read-only connection with external access off, returns 1-200 rows
  (largest is 55), contains no clock functions, and returns identical results on two runs.
- The project's own validator and golden-set check (guard, `EXPLAIN`, determinism, row cap) report
  no problems for all 36 queries.
- **Double-checked: all 36, reproducibly.** `golden/crosscheck.py` re-derives every reference by
  plain Python over the raw tables (own loops, sets and sorting, no shared SQL) and compares with
  the scorer's own equality rule; it is part of the test suite. 36/36 match, including the revised
  sessionization-1/-2.
- **Non-degenerate:** `analyst golden` flags a reference whose result is all NULL, all zero or
  identical rows; none of the 36 is.
- **Scorer audit:** `golden/audit_scorer.py` checks the equality rule on 575 rewritten variants of
  the 36 real results (re-typed numbers, dates as midnight timestamps, shuffled rows, perturbed and
  dropped cells, swapped columns, NULL changes). Every expected-equal variant was equal and every
  expected-different one was different.
- **Tie sensitivity** (tie_safe_ranking-1, -2, -3): re-running with the opposite event-id
  tie-break changes the result for all three, so these questions do detect an unsafe ranking.
- **Rounding**: for lag_lead-2, -3, date_spine-3, sessionization-2, pivot-3, conditional_aggregation-1
  and -2 the unrounded values were checked for distance from a rounding boundary; the closest
  is pivot-3 at 2.3% of the last digit, far above float noise.
- Sessionization: views inside a visit are 5 s to 20 min apart and visits are hours apart, so a
  30-minute rule only counts visits. All three questions use a threshold inside the intra-visit range
  (10 or 15 minutes), where the data holds gaps below, exactly at and above it across many customers.

## Review checklist for the owner

- [ ] Read the revenue definition and confirm it (completed orders only, `quantity * unit_price`).
- [ ] Read all 36 question texts as an analyst who has only the schema: is there exactly one
      correct result set for each?
- [ ] Skim the reference SQL of the six examples (they will appear in fixtures).
- [ ] Confirm the six example qids and that they are the ones you are happy to expose.
- [ ] Confirm the difficulty spread per pattern (-1 easy, -2 medium, -3 hard) feels right.
- [ ] Decide on the weak spots below (keep, rewrite or swap).
- [ ] Only then freeze digests, then sign `EVAL_PLAN.md`.

### Questions I consider weak or worth a second look

- ~~**sessionization-1 and -2**: with a 30-minute threshold the data barely exercises the rule.~~
  Resolved 2026-10-25: both moved to a 10-minute rule (see their rows). The shared warehouse was not
  changed, so its provenance (sql-cookbook, seed 20260830) is intact.
- **recursive_hierarchy-1**: tiny result (9 ids) and a very small org; easy by design.
- **running_total-3**: only five customers qualify, so a single wrong customer moves accuracy a lot
  within the question (but the question is still scored all-or-nothing).
- **gaps_and_islands-2 (55 rows) and pivot-2 (12 x 9)**: large result sets; one wrong cell fails the
  question. Intended, but they may be harsher than their "medium" tag suggests.
- **lag_lead-1**: the first row's gap is NULL; the scorer treats NULL as equal only to NULL
  (verified by the scorer audit).
- **Date and month outputs**: a DATE equals the same day as a midnight TIMESTAMP or as ISO text
  (verified). A question asking for "text in YYYY-MM form" is not met by a full date
  (`2025-03-01`): the question fixes the form, so that stays wrong by design.
- **pivot-1/-2/-3 vs conditional_aggregation-1/-2/-3**: they overlap in technique by design (the
  recipes do too); the pivot questions only differ in producing a wide, fixed-column result.
