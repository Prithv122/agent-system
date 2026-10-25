# Execution accuracy by arm

> Model: ollama qwen2.5-coder:7b-instruct-q3_K_M (digest bb739a02927e), temperature 0.0, max_tokens 1024, num_ctx 8192, seed None
> Value-hint set sha256: 31aa2ee4ecb4

> Value hints were on in the schema text for every arm.

## Per arm

| arm | n | EX correct | EX % | mean writer attempts | mean LLM calls | mean tokens (prompt+completion) | median model latency s | p95 model latency s | EX passes via column permutation |
|---|---|---|---|---|---|---|---|---|---|
| A | 36 | 9 | 25.0% | 1.00 | 1.00 | 702 | 37.24 | 100.22 | 0 |
| B | 36 | 9 | 25.0% | 1.31 | 1.31 | 1000 | 44.75 | 126.64 | 0 |
| C | 36 | 10 | 27.8% | 1.47 | 1.47 | 1217 | 44.75 | 126.64 | 0 |

Latency is model time only, summed per question: engine steps (guard, EXPLAIN, execution) record 0, so it is not end-to-end time. p95 is nearest-rank.

## Paired comparisons

| pair (x -> y) | b: x right, y wrong | c: x wrong, y right | McNemar exact p | EX diff (y - x), 95% bootstrap CI |
|---|---|---|---|---|
| A -> B | 0 | 0 | 1.0000 | +0.0 pp [+0.0, +0.0] |
| A -> C | 0 | 1 | 1.0000 | +2.8 pp [+0.0, +8.3] |
| B -> C | 0 | 1 | 1.0000 | +2.8 pp [+0.0, +8.3] |

Bootstrap: 10,000 resamples of the 36 questions, seed 20260830. Exact two-sided McNemar on the discordant pairs.

## EX by pattern

| pattern | A | B | C |
|---|---|---|---|
| conditional_aggregation | 3/3 (100.0%) | 3/3 (100.0%) | 3/3 (100.0%) |
| date_spine | 0/3 (0.0%) | 0/3 (0.0%) | 1/3 (33.3%) |
| gaps_and_islands | 0/3 (0.0%) | 0/3 (0.0%) | 0/3 (0.0%) |
| lag_lead | 0/3 (0.0%) | 0/3 (0.0%) | 0/3 (0.0%) |
| pivot | 1/3 (33.3%) | 1/3 (33.3%) | 1/3 (33.3%) |
| ranking | 1/3 (33.3%) | 1/3 (33.3%) | 1/3 (33.3%) |
| recursive_hierarchy | 1/3 (33.3%) | 1/3 (33.3%) | 1/3 (33.3%) |
| running_total | 0/3 (0.0%) | 0/3 (0.0%) | 0/3 (0.0%) |
| sessionization | 1/3 (33.3%) | 1/3 (33.3%) | 1/3 (33.3%) |
| subscription_lapses | 1/3 (33.3%) | 1/3 (33.3%) | 1/3 (33.3%) |
| tie_safe_ranking | 1/3 (33.3%) | 1/3 (33.3%) | 1/3 (33.3%) |
| top_n_per_group | 0/3 (0.0%) | 0/3 (0.0%) | 0/3 (0.0%) |

## Outcomes

| arm | ok | failed_validation | failed_execution | refused_unsafe | budget_exhausted |
|---|---|---|---|---|---|
| A | 24 | 10 | 1 | 1 | 0 |
| B | 25 | 5 | 1 | 1 | 4 |
| C | 26 | 2 | 1 | 1 | 6 |

## Failure kinds

| arm | syntax | execution | unsafe | no_sql | wrong_grain | wrong_values |
|---|---|---|---|---|---|---|
| A | 10 | 1 | 1 | 0 | 7 | 8 |
| B | 5 | 1 | 1 | 4 | 8 | 8 |
| C | 2 | 1 | 1 | 6 | 8 | 8 |
