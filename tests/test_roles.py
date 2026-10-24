from __future__ import annotations

import json

from analyst._vendor.results import Column, ResultSet
from analyst.roles import (
    explainer_prompt,
    parse_writer_reply,
    question_from_prompt,
    writer_prompt,
)

SCHEMA = "customers(customer_id INTEGER, name VARCHAR)"


def test_prompt_contains_question_block_and_schema() -> None:
    prompt = writer_prompt("How many customers?", SCHEMA, [])
    assert "QUESTION:\nHow many customers?\n" in prompt
    assert SCHEMA in prompt
    assert "PREVIOUS ATTEMPTS" not in prompt


def test_prompt_shows_history() -> None:
    prompt = writer_prompt("q?", SCHEMA, [("SELECT bad FROM t", "Binder Error: bad")])
    assert "PREVIOUS ATTEMPTS" in prompt
    assert "SELECT bad FROM t" in prompt and "Binder Error: bad" in prompt


def test_question_roundtrip_with_and_without_history() -> None:
    q = "What is the revenue by country?"
    assert question_from_prompt(writer_prompt(q, SCHEMA, [])) == q
    assert question_from_prompt(writer_prompt(q, SCHEMA, [("SELECT 1", "err")])) == q
    assert question_from_prompt("no marker here") is None


def test_parse_sql_action() -> None:
    a = parse_writer_reply(json.dumps({"action": "sql", "sql": " SELECT 1 "}))
    assert (a.kind, a.sql, a.detail) == ("sql", "SELECT 1", "")


def test_parse_sql_action_inside_json_fence() -> None:
    a = parse_writer_reply('```json\n{"action": "sql", "sql": "SELECT 2"}\n```')
    assert a.kind == "sql" and a.sql == "SELECT 2"


def test_parse_get_schema_and_refuse() -> None:
    assert parse_writer_reply('{"action": "get_schema"}').kind == "get_schema"
    r = parse_writer_reply('{"action": "refuse", "reason": "no such data"}')
    assert r.kind == "refuse" and r.detail == "no such data"


def test_fenced_sql_fallback() -> None:
    a = parse_writer_reply("Here you go:\n```sql\nSELECT count(*) FROM customers\n```\n")
    assert a.kind == "sql" and a.sql == "SELECT count(*) FROM customers"
    assert a.detail == "fenced fallback"


def test_invalid_replies() -> None:
    for text in (
        "",
        "I think the answer is 42",
        "[1, 2]",
        '{"action": "dance"}',
        '{"action":"sql"}',
    ):
        a = parse_writer_reply(text)
        assert a.kind == "invalid" and a.sql is None and a.detail


def test_explainer_prompt_counts_and_truncates() -> None:
    result = ResultSet(columns=(Column("n", "INTEGER"),), rows=tuple((i,) for i in range(120)))
    prompt = explainer_prompt("How many?", "SELECT n FROM t", result, max_rows=50)
    assert "ROW COUNT: 120 (first 50 shown)" in prompt
    assert "QUESTION:\nHow many?\n" in prompt
    assert "\n49\n" in prompt and "\n50\n" not in prompt


def test_schema_requested_adds_a_note_and_changes_the_prompt() -> None:
    plain = writer_prompt("q?", SCHEMA, [])
    after = writer_prompt("q?", SCHEMA, [], schema_requested=True)
    assert after != plain
    assert "SCHEMA block above is the complete schema" in after
    assert "complete schema" not in plain
    # the question is still recoverable and the note sits before the closing instruction
    assert question_from_prompt(after) == "q?"
    assert after.rstrip().endswith("Reply with the JSON object now.")


def test_first_prompt_never_mentions_the_attempt_budget() -> None:
    prompt = writer_prompt("q?", SCHEMA, [])
    for word in ("attempt budget", "attempts left", "last attempt", "max_attempts", "one attempt"):
        assert word not in prompt.lower()
