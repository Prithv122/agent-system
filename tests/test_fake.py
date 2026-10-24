from __future__ import annotations

from pathlib import Path

import pytest

from analyst._vendor.providers import LLMProvider, ProviderError
from analyst.executor import open_readonly
from analyst.fake import GoldenFakeProvider, ScriptedProvider, demo_provider
from analyst.loop import answer
from analyst.models import GoldenQuestion
from analyst.roles import EXPLAINER_SYSTEM, WRITER_SYSTEM, parse_writer_reply, writer_prompt

QUESTIONS = [
    GoldenQuestion(
        "q1", "How many customers are there?", "count", "SELECT count(*) FROM customers"
    ),
    GoldenQuestion(
        "q2", "How many orders are there?", "count", "SELECT count(*) AS n FROM orders;"
    ),
]


@pytest.fixture
def con(warehouse_path: Path):
    con = open_readonly(warehouse_path)
    yield con
    con.close()


def _ask(provider: GoldenFakeProvider, q: GoldenQuestion):
    prompt = writer_prompt(q.question, "t(a INTEGER)", [])
    return provider.complete(prompt, system=WRITER_SYSTEM, json_object=True)


def test_providers_satisfy_the_protocol() -> None:
    assert isinstance(ScriptedProvider([]), LLMProvider)
    assert isinstance(GoldenFakeProvider(QUESTIONS), LLMProvider)
    assert ScriptedProvider([]).name == GoldenFakeProvider([]).name == "fake"


def test_scripted_provider_replays_in_order_and_records_prompts() -> None:
    p = ScriptedProvider(["a", "b"])
    assert p.complete("one").text == "a"
    assert p.complete("two").text == "b"
    assert p.prompts == ["one", "two"]
    with pytest.raises(ProviderError):
        p.complete("three")


def test_golden_fake_returns_reference_sql() -> None:
    action = parse_writer_reply(_ask(GoldenFakeProvider(QUESTIONS), QUESTIONS[1]).text)
    assert action.kind == "sql" and action.sql == "SELECT count(*) AS n FROM orders;".rstrip(";")


def test_fault_counts_are_per_question_and_reset(con) -> None:
    provider = GoldenFakeProvider(QUESTIONS, faults={"q1": 2})

    def kinds(q: GoldenQuestion, n: int) -> list[str]:
        out = []
        for _ in range(n):
            sql = parse_writer_reply(_ask(provider, q).text).sql
            out.append("bad" if "no_such_column" in sql else "good")
        return out

    assert kinds(QUESTIONS[0], 4) == ["bad", "bad", "good", "good"]
    assert kinds(QUESTIONS[1], 2) == ["good", "good"]  # q2 has no faults, q1 did not leak
    provider.reset()
    assert kinds(QUESTIONS[0], 3) == ["bad", "bad", "good"]


def test_fault_sql_parses_but_fails_explain(con) -> None:
    provider = GoldenFakeProvider(QUESTIONS, faults={"q2": 1})
    trace = answer(QUESTIONS[1].question, provider, con, max_attempts=1)
    assert trace.outcome == "failed_validation"
    assert "no_such_column" in trace.steps[1].error


def test_golden_fake_drives_the_loop_end_to_end(con) -> None:
    provider = GoldenFakeProvider(QUESTIONS, faults={"q1": 1})
    one_shot = answer(QUESTIONS[0].question, provider, con, max_attempts=1)
    assert one_shot.outcome == "failed_validation"
    provider.reset()
    repaired = answer(QUESTIONS[0].question, provider, con, max_attempts=2)
    assert repaired.outcome == "ok" and repaired.attempts == 2
    assert repaired.explanation == "The query returned 1 row."


def test_unknown_question_is_refused() -> None:
    provider = GoldenFakeProvider(QUESTIONS)
    prompt = writer_prompt("What is the meaning of life?", "t(a INTEGER)", [])
    action = parse_writer_reply(provider.complete(prompt, system=WRITER_SYSTEM).text)
    assert action.kind == "refuse"


def test_explainer_reply_counts_rows() -> None:
    provider = GoldenFakeProvider(QUESTIONS)
    reply = provider.complete("QUESTION:\nq\n\nROW COUNT: 36\nROWS:\n", system=EXPLAINER_SYSTEM)
    assert reply.text == "The query returned 36 rows."


def test_demo_provider_repairs_a_misspelled_column(con) -> None:
    trace = answer("How many customers are there in each segment?", demo_provider(), con)
    assert trace.outcome == "ok" and trace.attempts == 2
    assert "customer_nme" in trace.steps[1].error
    assert trace.explanation
