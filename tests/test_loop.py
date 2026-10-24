from __future__ import annotations

import json
from pathlib import Path

import pytest

from analyst._vendor.providers import ProviderError
from analyst.executor import open_readonly
from analyst.fake import ScriptedProvider
from analyst.loop import answer

GOOD = "SELECT segment, count(*) AS n FROM customers GROUP BY segment ORDER BY segment"
BAD = "SELECT customer_nme FROM customers"
Q = "How many customers are in each segment?"


def sql_reply(sql: str) -> str:
    return json.dumps({"action": "sql", "sql": sql})


@pytest.fixture
def con(warehouse_path: Path):
    con = open_readonly(warehouse_path)
    yield con
    con.close()


def _order_count(con) -> int:
    return con.execute("SELECT count(*) FROM orders").fetchone()[0]


def test_first_try_success_with_explanation(con) -> None:
    provider = ScriptedProvider([sql_reply(GOOD), "Three segments."])
    trace = answer(Q, provider, con)
    assert trace.outcome == "ok" and trace.attempts == 1
    assert trace.final_sql == GOOD and trace.result is not None
    assert trace.explanation == "Three segments."
    assert [s.kind for s in trace.steps] == ["writer", "validate", "execute", "explain"]
    assert trace.llm_calls == 2


def test_schema_is_always_in_the_writer_prompt(con) -> None:
    provider = ScriptedProvider([sql_reply(GOOD)])
    answer(Q, provider, con, explain=False)
    assert "customers(customer_id INTEGER" in provider.prompts[0]


def test_hallucinated_column_is_caught_then_repaired(con) -> None:
    provider = ScriptedProvider([sql_reply(BAD), sql_reply(GOOD), "ok"])
    trace = answer(Q, provider, con)
    assert trace.outcome == "ok" and trace.attempts == 2
    assert trace.steps[1].kind == "validate" and "customer_nme" in trace.steps[1].error
    # the error was fed back to the writer on the second call
    assert "customer_nme" in provider.prompts[1] and "Candidate bindings" in provider.prompts[1]
    assert "PREVIOUS ATTEMPTS" not in provider.prompts[0]


def test_fails_twice_then_repairs_within_three_attempts(con) -> None:
    script = [sql_reply(BAD), sql_reply(BAD), sql_reply(GOOD), "ok"]
    trace = answer(Q, ScriptedProvider(script), con, max_attempts=3)
    assert trace.outcome == "ok" and trace.attempts == 3


def test_same_script_with_two_attempts_fails_validation(con) -> None:
    script = [sql_reply(BAD), sql_reply(BAD), sql_reply(GOOD), "ok"]
    provider = ScriptedProvider(script)
    trace = answer(Q, provider, con, max_attempts=2)
    assert trace.outcome == "failed_validation" and trace.attempts == 2
    assert trace.final_sql is None and trace.result is None
    assert len(provider.prompts) == 2  # no explainer, no third writer call


def test_one_attempt_means_no_retry(con) -> None:
    provider = ScriptedProvider([sql_reply(BAD), sql_reply(GOOD)])
    trace = answer(Q, provider, con, max_attempts=1)
    assert trace.outcome == "failed_validation" and trace.attempts == 1
    assert len(provider.prompts) == 1


@pytest.mark.parametrize(
    "sql",
    ["DELETE FROM orders", "DROP TABLE orders", "SELECT 1; DELETE FROM orders"],
)
def test_writes_are_refused_unsafe_and_data_is_unchanged(con, sql: str) -> None:
    before = _order_count(con)
    provider = ScriptedProvider([sql_reply(sql), sql_reply(GOOD)])
    trace = answer(Q, provider, con, max_attempts=3)
    assert trace.outcome == "refused_unsafe"
    assert trace.attempts == 1 and len(provider.prompts) == 1  # never coached around the guard
    assert trace.steps[-1].kind == "guard" and trace.steps[-1].error
    assert trace.result is None
    assert _order_count(con) == before


def test_runtime_error_is_fed_back_and_repaired(con) -> None:
    # passes EXPLAIN, fails at run time
    runtime_bad = "SELECT CAST(name AS INTEGER) FROM customers"
    provider = ScriptedProvider([sql_reply(runtime_bad), sql_reply(GOOD), "ok"])
    trace = answer(Q, provider, con)
    assert trace.outcome == "ok" and trace.attempts == 2
    execute = [s for s in trace.steps if s.kind == "execute"]
    assert execute[0].error and execute[1].error is None
    assert execute[0].error[:40] in provider.prompts[1]


def test_runtime_error_without_budget_is_failed_execution(con) -> None:
    provider = ScriptedProvider([sql_reply("SELECT CAST(name AS INTEGER) FROM customers")])
    trace = answer(Q, provider, con, max_attempts=1)
    assert trace.outcome == "failed_execution" and trace.attempts == 1


def test_timeout_is_failed_execution(con) -> None:
    slow = "SELECT count(*) FROM range(100000) a, range(100000) b, range(100000) c"
    trace = answer(Q, ScriptedProvider([sql_reply(slow)]), con, max_attempts=1, timeout_s=0.5)
    assert trace.outcome == "failed_execution"
    assert "interrupted" in trace.steps[-1].error


def test_get_schema_is_free_once(con) -> None:
    script = ['{"action": "get_schema"}', sql_reply(GOOD)]
    trace = answer(Q, ScriptedProvider(script), con, explain=False)
    assert trace.outcome == "ok" and trace.attempts == 1
    assert [s.kind for s in trace.steps] == [
        "writer",
        "get_schema",
        "writer",
        "validate",
        "execute",
    ]
    assert "customers(" in trace.steps[1].output


def test_second_get_schema_counts_as_invalid(con) -> None:
    gs = '{"action": "get_schema"}'
    provider = ScriptedProvider([gs, gs, sql_reply(GOOD)])
    trace = answer(Q, provider, con, max_attempts=2, explain=False)
    assert trace.outcome == "ok" and trace.attempts == 2
    assert trace.steps[2].error  # the second request was marked invalid
    assert sum(1 for s in trace.steps if s.kind == "get_schema") == 1


def test_invalid_reply_uses_an_attempt_and_is_retried(con) -> None:
    provider = ScriptedProvider(["I would use a join.", sql_reply(GOOD)])
    trace = answer(Q, provider, con, max_attempts=2, explain=False)
    assert trace.outcome == "ok" and trace.attempts == 2
    assert trace.steps[0].error and "PREVIOUS ATTEMPTS" in provider.prompts[1]


def test_only_invalid_replies_exhaust_the_budget(con) -> None:
    provider = ScriptedProvider(["nonsense", "more nonsense", "still nothing"])
    trace = answer(Q, provider, con, max_attempts=3)
    assert trace.outcome == "budget_exhausted" and trace.attempts == 3
    assert trace.result is None


def test_fenced_sql_fallback_is_accepted(con) -> None:
    provider = ScriptedProvider([f"```sql\n{GOOD}\n```"])
    trace = answer(Q, provider, con, explain=False)
    assert trace.outcome == "ok" and trace.final_sql == GOOD


def test_writer_refusal_ends_with_budget_exhausted(con) -> None:
    provider = ScriptedProvider(
        ['{"action": "refuse", "reason": "not in the data"}', sql_reply(GOOD)]
    )
    trace = answer(Q, provider, con, max_attempts=3)
    assert trace.outcome == "budget_exhausted"
    assert trace.steps[0].error == "writer refused: not in the data"
    assert len(provider.prompts) == 1


def test_explain_false_has_no_explainer_step(con) -> None:
    provider = ScriptedProvider([sql_reply(GOOD)])
    trace = answer(Q, provider, con, explain=False)
    assert trace.explanation is None
    assert all(s.kind != "explain" for s in trace.steps)
    assert len(provider.prompts) == 1


def test_trace_sums_tokens_and_latency(con) -> None:
    provider = ScriptedProvider([sql_reply(BAD), sql_reply(GOOD), "ok"])
    trace = answer(Q, provider, con)
    llm = [s for s in trace.steps if s.kind in ("writer", "explain")]
    assert len(llm) == 3 == trace.llm_calls
    assert trace.prompt_tokens == sum(s.prompt_tokens for s in llm) > 0
    assert trace.completion_tokens == sum(s.completion_tokens for s in llm) > 0
    assert trace.latency_s == sum(s.latency_s for s in trace.steps)
    assert all(s.prompt_tokens == 0 for s in trace.steps if s.kind not in ("writer", "explain"))


def test_writer_calls_use_temperature_zero_and_json_mode(con) -> None:
    calls: list[dict] = []

    class Spy(ScriptedProvider):
        def complete(self, prompt, **kw):
            calls.append(kw)
            return super().complete(prompt, **kw)

    answer(Q, Spy([sql_reply(GOOD), "ok"]), con)
    assert calls[0]["temperature"] == 0.0 and calls[0]["json_object"] is True
    assert calls[1]["temperature"] == 0.0 and not calls[1].get("json_object")


def test_provider_error_propagates(con) -> None:
    with pytest.raises(ProviderError):
        answer(Q, ScriptedProvider([]), con)
    with pytest.raises(ProviderError):
        answer(Q, ScriptedProvider([sql_reply(GOOD)]), con)  # explainer call has no reply left


def test_row_limit_is_applied(con) -> None:
    trace = answer(
        "list orders",
        ScriptedProvider([sql_reply("SELECT * FROM orders")]),
        con,
        explain=False,
        row_limit=7,
    )
    assert trace.result is not None and len(trace.result.rows) == 7
