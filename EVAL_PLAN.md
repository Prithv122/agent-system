# Evaluation plan — does the validate-and-repair loop raise execution accuracy?

**Status: DRAFT, not signed.** Every value marked **[OWNER]** is a proposal awaiting the owner's
decision. Once signed, this file is frozen before the first real model call on a held-out golden
question; later changes go in a dated amendment at the end that says what was known when it was
written.

## 1 Question

On the 36-question golden set over the seeded warehouse, does arm C (up to 3 writer attempts, each
failed attempt answered with the validator's or engine's error text) reach a higher execution
accuracy than arm A (one attempt, no feedback), and what does each extra attempt cost in model
calls, tokens and latency?

## 2 Data

- Warehouse: the seeded synthetic warehouse from sql-cookbook (seed 20260830, 7 tables, 3,000
  orders), built by `analyst build-db`, opened read-only.
- Golden set: `golden/questions.jsonl`, 12 patterns x 3 questions. Reference SQL and questions are
  approved by the owner (`golden/AUTHORING.md`) and frozen by `analyst golden --freeze` (a result
  digest per question; any drift aborts a run). 6 rows marked `example` may appear in test
  fixtures; the other 30 are held out from every prompt.

## 3 Arms and model

| Arm | Max writer attempts | Feedback |
|---|---|---|
| A | 1 | none: a validation failure ends the question |
| B | 2 | guard / `EXPLAIN` / engine error text shown to the writer |
| C | 3 | same as B |

- Same model, prompt, temperature 0 and JSON mode in every arm. Writer prompts are identical for
  the first attempt in all three arms, so the replay cache serves arm A's first attempt to B and C:
  the arms are **nested**, and any difference between them comes only from repair attempts.
- **Primary model [OWNER]:** proposed `qwen-coder-7b` (local `qwen2.5-coder:7b-instruct-q3_K_M`):
  free, no egress, replays offline. Secondary, reported, not confirmatory: `qwen-7b`, and
  `nemotron-super` (OpenRouter free tier) if the daily cap allows.
- No fallback model. A provider error stops the run; it is never scored as a wrong answer.

## 4 Scoring (fixed before any real run)

- **Execution accuracy (EX):** the agent's final result equals the reference result: values only,
  column names and types ignored, multiset unless the row says `order_matters`, numeric tolerance
  1e-6 relative, NULL equals NULL. Numeric comparison is type-aware (a DECIMAL and a DOUBLE compare
  as numbers; a VARCHAR is never coerced).
- **Column order [OWNER]:** proposed: a permutation of the expected columns counts as equal (the
  question names the columns, not their order). Extra or missing columns are wrong.
- **Value hints in the schema [OWNER]:** a smoke test showed a 7B model writing
  `status = 'Cancelled'` against data holding `'cancelled'`: valid SQL, wrong answer, nothing for
  the loop to repair. Proposed: list the distinct values of low-cardinality text columns (<= 12
  values) in the schema text, for every arm. It raises accuracy in all arms equally and keeps the
  loop's effect measurable on errors it can see; without it, the null result would be partly a
  prompt artefact.
- **Lenient parsing, same for every arm:** a reply that is not JSON but contains a fenced SQL block
  is accepted as SQL.
- **Attempt accounting:** each writer reply uses one attempt (SQL, unparseable, or a repeated schema
  request); one schema request per question is free. A guard refusal ends the question at once
  (outcome `refused_unsafe`); the agent never coaches a writer around the guard.
- Failure taxonomy: syntax, execution, unsafe, no_sql, wrong_columns, wrong_grain, wrong_values,
  wrong_order.

## 5 Decision rule

- **Primary [OWNER]:** proposed: the loop "helps" if EX(C) − EX(A) ≥ **10 points** and the 95%
  paired bootstrap CI of that difference (10,000 resamples of questions, seed 20260830) excludes 0.
- Reported alongside, not confirmatory: exact McNemar p for A vs C and A vs B; per-pattern EX;
  outcome and failure-kind counts; mean writer attempts, model calls and tokens per question;
  median and p95 latency.
- **Power, stated up front:** with 36 questions, McNemar needs at least 6 discordant pairs all in
  one direction to reach p < 0.05. A small or null difference is a likely, publishable result: it
  would mean the loop mostly fixes syntax slips that a strong model rarely makes.

## 6 Other measurements

- **Safety suite** (`golden/safety.jsonl`): required 0 writes executed and 0 files created,
  checked by the database file hash, row counts and the temp directory; run with the scripted
  writer and with the primary model.
- **Explainer faithfulness:** share of explanations in which every stated number is supported by
  the result rows (deterministic rules in `src/analyst/faithfulness.py`), on arm C's `ok` answers.

## 7 Sign-off

- [ ] Golden set reviewed and approved (`golden/AUTHORING.md` checklist)
- [ ] Primary model chosen
- [ ] Column-order rule confirmed
- [ ] Value hints in the schema decided
- [ ] Threshold confirmed

Signed: ________ (owner) · Date: ________
