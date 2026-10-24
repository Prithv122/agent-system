# Build Notes — SQL Analyst Agent

Working notes: what broke, what was tried, why X over Y.

---

## Log

### 2026-10-25 — build day 1

- **Built:** scaffold; vendored code pinned to its commits (guard from query-service `6e1900c`,
  warehouse + result capture + 12 recipes from sql-cookbook `ffd49cf`, provider stack from
  llm-eval-harness `07354f1`); shared contracts (`models.py`, CLI surface); the loop, executor,
  validator, roles and fakes; the eval harness (golden load/check/freeze, scorer, stats, runner,
  report); a 36-question golden set **drafted for owner review**.
- **Provider stack came from llm-eval-harness, not production-rag** (the brief named
  production-rag). That copy already removed the silent fallback chain, which matters here for the
  same reason it did for a judge: the model under test must never be swapped mid-run.
- **Read-only is enforced three times.** The guard (one statement, SELECT/WITH only), the
  connection (`read_only=True`), and `enable_external_access = false` followed by
  `SET lock_configuration = true`, so SQL cannot turn file access back on. Without the last two, a
  plain `SELECT * FROM read_csv('C:/anything')` passes the guard (it is a SELECT) and reads a local
  file: the guard alone would have been an exfiltration hole.
- **Broke: a dynamic PIVOT is "unsafe".** DuckDB plans `PIVOT t ON c USING ...` without an `IN`
  list as two statements (one to find the distinct values), so the guard reports `MULTI` and
  refuses it. With an explicit `IN (...)` list it is one SELECT and passes. Loosening the guard for
  PIVOT was rejected; the writer prompt now says to list the values. The same prompt goes to every
  arm, so the comparison is unaffected, but a model that ignores the rule is scored `unsafe` for a
  harmless query — worth counting separately when reading real results.
- **Broke: `date_trunc` returns TIMESTAMP even for a DATE.** A correct monthly answer arrived as
  `2024-03-01 00:00:00` against a reference `2024-03-01`. The scorer now treats a TIMESTAMP at
  exactly midnight as its date (values only, types ignored). A non-midnight timestamp still differs.
- **Column permutation is allowed** in result equality: the question names the columns but rarely
  fixes their order. It is applied identically to every arm and needs owner sign-off in
  `EVAL_PLAN.md`.
- **Arms are nested by construction.** The writer prompt never mentions the attempt budget, so the
  first attempt is byte-identical in A, B and C and the replay cache serves it once. Any difference
  between arms comes only from repair attempts, and a full run costs at most 36 x 3 writer calls.
- **Slow:** building the warehouse takes 20–60 s (the vendored loader inserts through large literal
  INSERT statements). Tests build it once per session.

---

## Rejected approaches

| Approach | Why rejected |
|---|---|
| An agent framework (LangGraph, CrewAI) | The loop is the thing being measured. Four fixed roles and a bounded retry are a state machine; a framework hides it and adds a dependency tree |
| Keyword blocklist for writes | A losing game; the database's own parser is asked how many statements there are and what kind |
| Coaching the writer after a guard refusal | A refusal ends the question. Feeding the guard's reason back teaches a writer how to get around it |
| Loosening the guard to allow dynamic PIVOT | A guard exception for one statement shape is where holes start; the prompt asks for an explicit list instead |
| Strict equality on column names and types | Would measure naming luck (`total` vs `revenue`), not correctness |

## Open questions

- [ ] Owner review of the golden set (`golden/AUTHORING.md`); sessionization-1/-2 hinge on one
      exact-30-minute gap
- [ ] Owner sign-off on `EVAL_PLAN.md` (primary model, column-order rule, threshold)
- [ ] TPC-H transfer set (needs the `tpch` extension download) — not started
