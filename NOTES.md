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
- **Smoke test, real local model (qwen2.5 7B, a question outside the golden set):** "How many
  orders were cancelled in 2025?" -> `WHERE status = 'Cancelled'`. The data says `'cancelled'`, so the
  query was valid, ran, and returned 0; the explainer then faithfully said "No orders were cancelled
  in 2025." Wrong answer, every check green. **The repair loop only sees errors the database
  reports**; a wrong literal is silent. Writer 33 s, explainer 13 s on this machine. This makes the
  schema-hint question an owner decision before the run (see `EVAL_PLAN.md` section 4).
- **Slow:** building the warehouse takes 20–60 s (the vendored loader inserts through large literal
  INSERT statements). Tests build it once per session.

### 2026-10-25 — independent review, then fixes

A reviewer with no context of the build found no path for writer SQL to write or read files
(about 30 bypass attempts against the locked connection), and these real defects, all fixed with a
test each:

- **Safety false PASS (high).** With a model, a run where every case hit a provider error (empty
  replay cache, Ollama down) printed PASS and exited 0: no model had been tested. Now INCOMPLETE,
  exit 2. A test had asserted the false PASS.
- **Resume mixed models (high).** Resume checked only the arm, so `eval --arm A` for a second model
  into the same folder served the first model's rows and rewrote the metadata. Now the sidecar
  (model, golden hash, settings) must match, outputs go to `<dir>/<model>/`, and `report` refuses
  mismatched arms and warns on incomplete or unfrozen runs.
- **The free schema request was a trap.** After `get_schema` the next prompt was identical, so a
  temperature-0 model asked again and burned an attempt: arm A lost the question while B and C
  recovered. That would have inflated C − A with a protocol artefact. The follow-up prompt now says
  the schema is complete.
- **Free column order accepted mislabelled pivots.** With names ignored, swapping two same-typed
  columns passed. Columns are now positional on the 34 questions that list their columns.
- Also: replay cache anchored to the project root, with export/import for a committed bundle; a run
  lock so arms run sequentially (parallel runs could race on arm A's first reply); outcome labels
  keep the last SQL failure; faithfulness no longer lets a date's day number support "17
  customers", a 0.31 cell support "0 percent", or a 1-row result support "100%"; the shown-row cap
  is a supported number; fixtures use the production connection config.
- **Not fixed, recorded:** execution outcomes are not cached, only model replies, so a query near
  the 10 s timeout could behave differently on replay (all 36 references run well under 1 s);
  `capture` rounds doubles to 6 places before the tolerance applies (irrelevant to this golden set).

### Local CI record — 2026-10-25, commit `5d92557`

Hosted CI was unavailable (no GitHub repo yet; the account's Actions limit runs to 2026-11-02), so
`.github/workflows/ci.yml` was replayed step by step on a fresh clone of `5d92557` with a fresh
install (`UV_NO_CACHE=1`, `SECRETS_NO_KEYRING=1`, `uv sync --all-groups --extra excel`): ruff check and
format clean; **345 passed**, coverage 92%; `build-db`; `golden` 36 OK; `ask --fake` ok; `safety --fake`
PASS; fake arms A and C on the 6 example rows (2/6 and 6/6, a pipeline check) and `report`.
Differences from the hosted runner: Windows 11 instead of Ubuntu, `--basetemp` in a scratch folder,
`-p no:cacheprovider`. The first replay (at `70e01bb`) failed one test on a CRLF checkout, which led to
`.gitattributes`.

### 2026-10-25 — owner review applied, pre-freeze checks

- Owner decisions (from two pasted reviews): `qwen-coder-7b`; positional columns on 34/36; value
  hints on, mechanical, identical across arms; primary rule = C > A with a paired 95% CI excluding 0,
  +10 points reported separately; revise sessionization-1/-2; audit normalisation; no freeze, sign
  or run until a pre-freeze report is confirmed.
- Sessionization moved to a 10-minute rule instead of changing the shared warehouse: the data's
  intra-visit gaps (5 s to 20 min) put 5,370 gaps below, 8 exactly at and 6,523 above 10 minutes.
- Added: `golden/crosscheck.py` (plain-Python recompute, 36/36), a degenerate-reference rule,
  `golden/audit_scorer.py` (575 variants, 0 surprises), and model pinning in every run's sidecar
  (Ollama digest `bb739a02927e...`, generation settings, hint set and hash).
- Open for the owner: arm C's budget (brief: 3 attempts; one review note said "three repairs"),
  `employees.title` in the hints (titles follow the reporting chain exactly, so title-based and
  chain-based VP lookups agree).
- Local CI on a fresh clone of `4e64b11`: ruff clean, **411 passed**, coverage 93%, build-db, golden
  36 OK, ask / safety / eval / report with the scripted writer green. Same differences from the
  hosted runner as before.

### 2026-10-25 — the pre-registered run

- Frozen and signed at `870570d`; arms run sequentially (A 26 min, B 8 min, C 6 min of wall time;
  B and C re-used A's cached first replies, as designed). One false start: arm B refused to begin
  because the digest lookup timed out right after arm A; no model had been called, retry was clean.
- **EX: A 9/36, B 9/36, C 10/36.** C − A = +2.8 pts [0.0, +8.3]; McNemar 0 vs 1, p = 1.0. Primary
  hypothesis not supported; practical target not met.
- **Why:** 15 of A's 27 failures were silent (valid SQL, wrong rows). Of the 10 `EXPLAIN` failures,
  C fixed 1 to correct and 1 to runnable-but-wrong; **6 ended with the writer using `refuse` to
  describe the error** ("The query uses a function 'TO_CHAR' which is not available in DuckDB...")
  instead of returning fixed SQL. The 7B model reads "refuse" as "explain". One guard refusal was a
  parse error (`ORDER BY` before `UNION ALL` inside a recursive CTE): the vendored guard turns a
  parse failure into `UnsafeQuery`, and the protocol ends a question on a guard refusal.
- Both are protocol artefacts that hold the loop back, found after results, so they are recorded and
  not changed. A follow-up would be a second pre-registered run: refusal allowed only on the first
  attempt, and parse errors routed to the repair path.
- **Faithfulness:** 23/26 by rule; the 3 flags are false positives of the checker (dates in words,
  list numbering), confirmed by reading the rows. A real slip a number check cannot see: "lapse
  ended on" used for the subscription's end date.
- **Replay broke twice, and both were real:** (1) faithfulness re-ran the model's SQL, whose row
  order is not fixed without `ORDER BY` (parallel aggregation), so the explainer prompt changed;
  fixed by using the recorded rows (Amendment A1). (2) 4 model-mode safety requests embed a per-run
  temp path, so their prompts can never replay; recorded, not fixed (the effect check does not
  depend on the prompt).
- Fresh-clone replay: arms A/B/C rows byte-identical, report identical, faithfulness 26/26 cached.

### Local CI record — 2026-10-25, commit `91e9a80` (after the results)

Fresh clone, fresh install (`UV_NO_CACHE=1`, `SECRETS_NO_KEYRING=1`), the workflow's steps in order:
ruff clean, **412 passed**, coverage 93%, build-db, golden 36 OK, ask / safety / eval / report with the
scripted writer green. README section 6 run verbatim on a fresh clone of `f535ed3`: the committed
cache imports (118 replies) and arms A/B/C replay to 9 / 9 / 10 of 36 with the same paired table,
faithfulness 23/26. That clone also caught a test still expecting an unfrozen golden file (fixed in
`91e9a80`). Hosted CI not run: no GitHub repo, Actions limit until 2026-11-02.

### 2026-11-01 — published; first hosted run red, fixed

- Published as public `Prithv122/agent-system`, `main` = `f217e8e`. Hosted run 41234573188 on that
  SHA **failed** at the last step: `eval --out <dir>` files runs under `<dir>/<model>/` since the
  review fixes, but the workflow's `report` line still read `<dir>/armA.jsonl`. Every earlier step
  (lint, 412 tests, build-db, golden, ask, safety) passed on Ubuntu.
- **Why local CI missed it:** the three earlier replays typed the eval/report commands by hand with
  the correct `fake/` path instead of running the workflow file. Fixed in `93e3747`. From now on the
  replay runs a script generated from the workflow's own `run:` blocks.
- Verbatim replay of `93e3747` on a fresh clone (script extracted from `ci.yml`; only
  `PYTEST_ADDOPTS=--basetemp` and `RUNNER_TEMP` set locally): ruff clean, **412 passed**, coverage
  93%, build-db, golden 36 OK, ask ok, safety PASS, fake arms A/C and report green.
- A clone pulled from GitHub at `f217e8e` (104 files) passes the README steps and replays A/B/C to
  9 / 9 / 10 of 36.
- Known and accepted: 4 safety prompts in `results/cache/llm.jsonl` carry the local temp path (with
  the Windows user name); they cannot replay anyway, and rewriting history would change SHAs cited
  in these notes.

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

- [x] Owner review of the golden set (done 2026-10-25; sessionization-1/-2 revised)
- [x] Owner sign-off on `EVAL_PLAN.md` (signed 2026-10-25, `870570d`)
- [ ] A second pre-registered run with the two artefacts fixed (refuse only on attempt 1; parse
      errors repairable) — a separate experiment, not a re-scoring of this one
- [ ] TPC-H transfer set (needs the `tpch` extension download) — not started
