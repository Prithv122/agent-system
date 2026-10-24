"""The agent loop: draft, validate, run, repair, explain.

This is the thing the evaluation measures, so it is kept small and linear. Per question the
writer gets a bounded number of attempts. Each attempt that returns SQL is checked by the guard
and by ``EXPLAIN``; an error is shown to the writer on its next attempt. The guard is different:
a refusal ends the question at once, because a writer must never be coached around it.

Provider errors propagate. An unreachable model is an infrastructure failure and must never be
scored as a wrong answer.
"""

from __future__ import annotations

import duckdb

from ._vendor.providers import LLMProvider, LLMResponse
from ._vendor.results import ResultSet
from .executor import ExecutionError, run
from .models import Outcome, Step, StepKind, Trace
from .roles import (
    EXPLAINER_SYSTEM,
    WRITER_SYSTEM,
    explainer_prompt,
    parse_writer_reply,
    writer_prompt,
)
from .schema import schema_text
from .validator import validate


def answer(
    question: str,
    provider: LLMProvider,
    con: duckdb.DuckDBPyConnection,
    *,
    max_attempts: int = 3,
    explain: bool = True,
    timeout_s: float = 10.0,
    row_limit: int | None = None,
) -> Trace:
    trace = Trace(question=question)
    schema = schema_text(con)
    history: list[tuple[str, str]] = []  # (sql, error) of failed attempts, shown to the writer
    schema_requested = False
    outcome: Outcome = "budget_exhausted"
    while trace.attempts < max_attempts:
        prompt = writer_prompt(question, schema, history)
        reply = provider.complete(prompt, system=WRITER_SYSTEM, temperature=0.0, json_object=True)
        action = parse_writer_reply(reply.text)
        writer = _llm_step("writer", prompt, reply)
        trace.steps.append(writer)
        if action.kind == "get_schema" and not schema_requested:
            schema_requested = True  # free once: the schema is already in the prompt
            trace.steps.append(Step("get_schema", output=schema))
            continue
        trace.attempts += 1
        if action.kind == "refuse":
            writer.error = f"writer refused: {action.detail}"
            outcome = "budget_exhausted"
            break
        if action.sql is None:
            writer.error = action.detail or "the reply did not contain a query"
            history.append(("", writer.error))
            outcome = "budget_exhausted"
            continue
        sql = action.sql
        check = validate(con, sql)
        step_kind: StepKind = "guard" if check.kind == "guard" else "validate"
        trace.steps.append(Step(step_kind, sql, error=check.error))
        if check.kind == "guard":
            outcome = "refused_unsafe"
            break
        if not check.ok:
            history.append((sql, check.error or "validation failed"))
            outcome = "failed_validation"
            continue
        try:
            result = run(con, sql, limit=row_limit, timeout_s=timeout_s)
        except ExecutionError as exc:
            trace.steps.append(Step("execute", sql, error=str(exc)))
            history.append((sql, str(exc)))
            outcome = "failed_execution"
            continue
        trace.steps.append(Step("execute", sql, output=f"{len(result.rows)} rows"))
        trace.final_sql, trace.result, outcome = sql, result, "ok"
        break
    trace.outcome = outcome
    if explain and trace.result is not None and trace.final_sql is not None:
        _explain(trace, provider, trace.final_sql, trace.result)
    return trace


def _explain(trace: Trace, provider: LLMProvider, sql: str, result: ResultSet) -> None:
    prompt = explainer_prompt(trace.question, sql, result)
    reply = provider.complete(prompt, system=EXPLAINER_SYSTEM, temperature=0.0)
    trace.steps.append(_llm_step("explain", prompt, reply))
    trace.explanation = reply.text.strip()


def _llm_step(kind: StepKind, prompt: str, reply: LLMResponse) -> Step:
    return Step(
        kind,
        input=prompt,
        output=reply.text,
        prompt_tokens=reply.prompt_tokens,
        completion_tokens=reply.completion_tokens,
        latency_s=reply.latency_s,
        cached=reply.cached,
    )
