# Evaluation plan — does the validate-and-repair loop raise execution accuracy?

**Status: DECIDED 2026-10-25, NOT YET SIGNED.** The owner's review decisions of 2026-10-25 are
written in below. Signing waits for the pre-freeze review report (`golden/PRE_FREEZE_REVIEW.md`) and
the owner's confirmation of the final configuration. Once signed, this file is frozen before the
first real model call on a held-out golden question; later changes go in a dated amendment at the
end that says what was known when it was written. No rule changes after any accuracy result is seen.

## 1 Question

On the 36-question golden set over the seeded warehouse, does arm C (up to 3 writer attempts, each
failed attempt answered with the validator's or engine's error text) reach a higher execution
accuracy than arm A (one attempt, no feedback), and what does each extra attempt cost in model
calls, tokens and latency?

## 2 Data

- Warehouse: the seeded synthetic warehouse from sql-cookbook (seed 20260830, 7 tables, 3,000
  orders), built by `analyst build-db`, opened read-only.
- Golden set: `golden/questions.jsonl`, 12 patterns x 3 questions. Owner decision: structure,
  revenue conventions, examples and difficulty spread approved; sessionization-1/-2 revised to a
  10-minute rule so the data holds gaps below, exactly at and above the boundary across several
  customers (the shared seeded warehouse is unchanged); the other 34 unchanged. Every reference is
  recomputed in plain Python by `golden/crosscheck.py` and must be non-degenerate (not all NULL,
  not all zero, not identical rows) unless tagged `degenerate_ok`. Frozen by `analyst golden
  --freeze` (a result digest per question; any drift aborts a run). 6 rows marked `example` may
  appear in test fixtures; the other 30 are held out from every prompt.

## 3 Arms and model

| Arm | Max writer attempts | Feedback |
|---|---|---|
| A | 1 | none: a validation failure ends the question |
| B | 2 | guard / `EXPLAIN` / engine error text shown to the writer |
| C | 3 | same as B |

- Arm A is one shot: no repair. B allows one repair, C two (attempts <= 3, as in the approved brief).
  A repair is triggered only by deterministic feedback: the `EXPLAIN` or engine error text, or the
  protocol message "the reply did not contain a query". The writer never sees the reference answer
  or anything derived from it; the scorer alone knows the gold result. A silent wrong answer (valid
  SQL, wrong rows) gets no feedback and stays wrong: the loop is measured as it would run in use.
- Same model, prompt, temperature 0 and JSON mode in every arm. Writer prompts are identical for
  the first attempt in all three arms, so the replay cache serves arm A's first attempt to B and C:
  the arms are **nested**, and any difference between them comes only from repair attempts.
- **Primary model (decided):** `qwen-coder-7b` (local `qwen2.5-coder:7b-instruct-q3_K_M`): free, no
  egress, replays offline. One fixed configuration for A, B and C, recorded in every run's sidecar:
  model id, Ollama model digest, temperature 0 (greedy decoding), writer `max_tokens`, JSON mode,
  context window 8192, seed (none sent; irrelevant under greedy decoding). Resume and `report` refuse
  runs whose configuration differs. Secondary, reported, not confirmatory: `qwen-7b`, and
  `nemotron-super` (OpenRouter free tier) if the daily cap allows.
- No fallback model and no substitution. A provider error stops the run; it is never scored as a
  wrong answer, and a failed run is resumed on the same configuration or restarted, never swapped.

## 4 Scoring (fixed before any real run)

- **Execution accuracy (EX):** the agent's final result equals the reference result: values only,
  column names and types ignored, multiset unless the row says `order_matters`, numeric tolerance
  1e-6 relative, NULL equals NULL. Numeric comparison is type-aware (a DECIMAL and a DOUBLE compare
  as numbers; a VARCHAR is never coerced).
- **Row order** (`order_matters`) and **column order** (`column_order_matters`) are independent
  rules. Row order is enforced only where the question asks for an ordering.
- **Column order (decided):** positional wherever the question lists its output columns in
  order (`column_order_matters`, set on 34 of 36 questions, including every pivot); a permutation is
  accepted only on the other two questions, which return a single column, so order and names cannot
  matter there. Reason: with names ignored, a free
  permutation would accept a pivot whose `consumer` and `smb` columns are swapped. Extra or missing
  columns are wrong. The report counts any pass that needed a permutation.
- **Value hints in the schema (decided, primary protocol):** a smoke test showed a 7B model writing
  `status = 'Cancelled'` against data holding `'cancelled'`: valid SQL, wrong answer, nothing for
  the loop to repair. Every arm's schema text lists the distinct values of each text column with at
  most 12 distinct non-null values, derived mechanically from the database (`schema.value_hint_set`),
  never chosen per question. The exact hint set and its sha256 go in each run's sidecar. Primary runs
  use `--value-hints`; a run without hints is a secondary analysis.
- **Lenient parsing, same for every arm:** a reply that is not JSON but contains a fenced SQL block
  is accepted as SQL.
- **Attempt accounting:** each writer reply uses one attempt (SQL, unparseable, or a repeated schema
  request); one schema request per question is free, and the next prompt says the schema shown is
  complete (otherwise a temperature-0 model asks again and a one-shot arm loses the question to a
  protocol artefact). When attempts run out, the outcome is that of the last attempt that produced
  SQL; `budget_exhausted` means no attempt produced SQL or the writer refused. A guard refusal ends the question at once
  (outcome `refused_unsafe`); the agent never coaches a writer around the guard.
- Failure taxonomy: syntax, execution, unsafe, no_sql, wrong_columns, wrong_grain, wrong_values,
  wrong_order.

## 5 Decision rule

- **Run procedure:** arms run one after another (a lock file enforces it), A then B then C, so arm
  A's first reply is cached once and served to B and C. Each run file carries a sidecar (model,
  golden file hash, settings); resume and `report` refuse anything that does not match. The cache is
  exported to `results/cache/llm.jsonl` and committed, so every number replays from a clean clone.
- **Primary hypothesis (decided):** arm C improves execution accuracy over arm A: the paired
  difference EX(C) − EX(A) is positive and its 95% paired bootstrap CI (10,000 resamples of
  questions, seed 20260830) excludes 0. Correctness is paired by question; no independent-proportion
  interval is used anywhere.
- **Practical-effect target, reported separately:** EX(C) − EX(A) ≥ +10 points. A supported gain
  below +10 is reported as "real but small", not as a failure; the two questions ("does the loop
  help?" and "is the gain large enough to matter?") are answered separately.
- Reported alongside, not confirmatory: exact McNemar p for A vs C and A vs B (if McNemar and the
  bootstrap disagree at 0.05, the result is called fragile); per-pattern EX;
  outcome and failure-kind counts; mean writer attempts, model calls and tokens per question;
  median and p95 latency.
- **Power, stated up front:** with 36 questions, McNemar needs at least 6 discordant pairs all in
  one direction to reach p < 0.05. A small or null difference is a likely, publishable result: it
  would mean the loop mostly fixes syntax slips that a strong model rarely makes.

## 6 Other measurements

- **Safety suite** (`golden/safety.jsonl`, 35 cases): required 0 writes executed and 0 files
  created, checked by the database file hash, row counts, the temp directory and a canary file; run
  with the scripted writer and with the primary model. In model mode a case with no model answer
  makes the run INCOMPLETE, never a pass.
- **Explainer faithfulness:** share of explanations in which every stated number is supported by
  the result rows (deterministic rules in `src/analyst/faithfulness.py`), on arm C's `ok` answers,
  via `analyst faithfulness`; its cost is reported apart from the EX cost columns.

## 7 Sign-off

- [x] Golden set structure approved; sessionization-1/-2 revision required (owner, 2026-10-25)
- [ ] Pre-freeze review report accepted (`golden/PRE_FREEZE_REVIEW.md`)
- [x] Primary model: `qwen-coder-7b` (owner, 2026-10-25)
- [x] Column order: positional on the questions that list columns (owner, 2026-10-25)
- [x] Value hints: on, mechanical, identical across arms, recorded (owner, 2026-10-25)
- [x] Decision rule: C > A with paired 95% CI excluding 0; +10 pts reported separately (owner, 2026-10-25)
- [ ] Final configuration confirmed, golden set frozen

Signed: ________ (owner) · Date: ________
