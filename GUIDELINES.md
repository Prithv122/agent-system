# SQL Analyst Agent — G5

**Tier:** 3 · **Category:** AI engineering · **Wave:** 4

The project's own engineering guide. Keep it short and current.

## What this is

A business user asks a data question in words. A writer model drafts SQL; a deterministic
validator checks it with DuckDB's own parser and `EXPLAIN` and hands any error back to the writer
(bounded attempts); the SQL then runs on a connection the database itself makes read-only; an
explainer states the answer without inventing numbers. The project's claim is a measurement:
**does the validate-and-repair loop raise execution accuracy over one-shot SQL, and what does each
extra attempt cost?** The "team" is two LLM roles plus deterministic checks, and the README says so.

## Stack

Python 3.12 · `uv` · DuckDB (read-only file, external access off) · stdlib HTTP providers with an
on-disk replay cache · no agent framework (the loop is the thing being measured, so it stays visible).
Vendored code is pinned in `src/analyst/_vendor/__init__.py`.

## Module map (who owns what)

| Module | Job |
|---|---|
| `models.py` | `GoldenQuestion`, `Step`, `Trace`, `EvalRow`, `ARMS`, outcomes, failure kinds |
| `llm.py` | named models -> cached provider stack (`--model`, `--replay`) |
| `executor.py` | `open_readonly(path)`, `build_db(path)`, `run(con, sql, *, limit, timeout_s)` |
| `validator.py` | `validate(con, sql) -> str | None` (guard, then `EXPLAIN`); `GuardRefusal` |
| `schema.py` | `schema_text(con, value_hints=)`, `value_hint_set` (the mechanical hint rule) |
| `roles.py` | writer and explainer prompts; `parse_writer_reply(text) -> WriterAction` |
| `loop.py` | `answer(question, provider, con, *, max_attempts, explain) -> Trace` |
| `fake.py` | scripted providers for tests and `--fake` (never a published number) |
| `golden.py` | load/validate/freeze the golden set |
| `score.py` | result-set equality and failure classification |
| `stats.py` | exact McNemar, paired bootstrap CI |
| `runner.py`, `report.py` | run an arm to JSONL; compare arms as markdown |
| `safety.py` | adversarial suite: proves zero writes |
| `faithfulness.py` | every number in an explanation traces to the result rows |
| `loader.py` | CSV/XLSX -> a new DuckDB file |
| `commands_*.py` | CLI handlers `cmd_<name>(args) -> int` |

## Non-negotiables

- The guard is never loosened: one statement, `SELECT`/`WITH` only, read-only connection,
  `enable_external_access = false`, row cap, timeout. A write that reaches the engine is a failed build.
- Result equality: values only, column names and types ignored, multiset unless `order_matters`,
  numeric tolerance 1e-6 relative. Changing that rule after a real run invalidates the run.
- Arms differ only in max attempts (A=1, B=2, C=3): same model, prompt, temperature 0.
- `EVAL_PLAN.md` is signed by the owner before the first real run; its threshold is not edited after.
- Reference SQL and questions are the owner's. Nothing invents EX, latency or cost numbers.
- Tests never call a model or the network (`SECRETS_NO_KEYRING=1`, fake providers only).

## Acceptance criteria

- [x] `analyst ask "<q>" --fake` prints a trace; hallucinated columns are caught by `EXPLAIN`
- [x] Golden set of 36 questions (12 patterns x 3) validates and freezes against the warehouse
- [x] Arms A/B/C run and `report` gives EX, discordant pairs, McNemar p, bootstrap CI, cost per arm
- [x] Safety suite: 0 writes executed, every adversarial request refused or rejected
- [x] Explainer numbers checked against the result rows
- [x] Runs from a clean clone using only the documented steps
- [x] Tests, lint and CI green; README results come from real runs

## Conventions

- `uv` for dependencies; `ruff check` + `ruff format`; `pytest`.
- No secrets in the repo. Keys come from the environment or the OS credential store.
- Local pytest needs `--basetemp` outside `%TEMP%` on this machine; never put it in `pyproject.toml`.

## Project-specific notes

- `analyst build-db` writes `data/warehouse.duckdb` (git-ignored). DuckDB cannot open an in-memory
  database read-only, so tests build a file under `tmp_path` and open it read-only.
- Real model runs are local and slow (a 7B model answers in tens of seconds); run them in the
  background and commit the replay bundle so the numbers replay offline.
