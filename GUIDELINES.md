# SQL Analyst Agent — G5

**Tier:** 3 · **Category:** AI engineering · **Wave:** 4

The project's own engineering guide. Keep it short and current.

## What this is

A question-to-SQL agent that checks its SQL with the database's own parser and EXPLAIN before running it read-only, and measures whether that repair loop actually raises execution accuracy

## Stack

_..._

## Acceptance criteria

- [ ] _the project's measurable goals_
- [ ] Runs from a clean clone using only the documented steps
- [ ] Tests, lint and CI green; README results come from real runs

## Conventions

- `uv` for dependencies; `ruff check` + `ruff format`; `pytest`.
- No secrets in the repo; `.env.example` only if the project reads env vars, listing the real ones.

## Project-specific notes

_Gotchas, service prereqs, dataset location, anything a newcomer needs and can't infer._
