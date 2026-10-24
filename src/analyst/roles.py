"""The two model roles: what they are told, and how the writer's reply is read.

The writer drafts SQL and answers in a small JSON protocol; the explainer turns a result into a
sentence or two. Prompts are plain on purpose. Every arm of the evaluation sends the same text
and parses replies with the same leniency, so the only thing that differs between arms is how
many attempts the loop allows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ._vendor.providers import extract_json
from ._vendor.results import ResultSet

WRITER_SYSTEM = """You are a careful data analyst. You answer a business question by writing \
one DuckDB SQL query over the tables listed under SCHEMA.

Rules:
- Write exactly one SELECT (or WITH ... SELECT) statement in DuckDB SQL. Never modify data.
- Use only the tables and columns listed in the schema. Do not invent names.
- Return only the columns the question asks for, and sort only when the question asks for an order.
- If you use PIVOT, list the pivot values explicitly (PIVOT ... ON col IN (...)); a PIVOT \
without that list runs as two statements and is rejected.
- If earlier attempts are listed, each failed with the error shown. Fix that error.

Reply with one JSON object and nothing else:
{"action": "sql", "sql": "<the query>"}
{"action": "get_schema"}            (ask to see the schema again)
{"action": "refuse", "reason": "<why>"}   (the question cannot be answered from this data, \
or asks you to change data)"""

EXPLAINER_SYSTEM = """You explain a query result to a business user in one to three plain \
sentences.

Rules:
- Use only numbers that appear in the rows you are given. Never compute new figures or guess.
- Say how many rows the query returned.
- If the result is empty, say so. If it is cut off, say that only the first rows are shown.
- Reply with the explanation text only."""

_QUESTION_BLOCK = re.compile(r"QUESTION:\n(.*?)\n(?:\n|\Z)", re.DOTALL)
_SQL_FENCE = re.compile(r"```sql\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

WriterKind = Literal["sql", "get_schema", "refuse", "invalid"]


@dataclass
class WriterAction:
    kind: WriterKind
    sql: str | None = None
    detail: str = ""


def writer_prompt(question: str, schema: str, history: list[tuple[str, str]]) -> str:
    """The writer's user message. ``history`` is (sql, error) for each failed earlier attempt."""
    parts = [f"SCHEMA:\n{schema}\n", f"QUESTION:\n{question.strip()}\n"]
    if history:
        attempts = [
            f"Attempt {i}:\n{sql.strip() or '(no usable SQL)'}\nError: {error}"
            for i, (sql, error) in enumerate(history, start=1)
        ]
        parts.append("PREVIOUS ATTEMPTS (all failed):\n" + "\n\n".join(attempts) + "\n")
    parts.append("Reply with the JSON object now.")
    return "\n".join(parts)


def question_from_prompt(prompt: str) -> str | None:
    """Recover the question from a writer or explainer prompt, or None if there is no block."""
    match = _QUESTION_BLOCK.search(prompt)
    return match.group(1).strip() if match else None


def parse_writer_reply(text: str) -> WriterAction:
    """Read the writer's reply. Same leniency for every arm: JSON, else a fenced ```sql block."""
    try:
        data = extract_json(text)
    except ValueError:
        fenced = _SQL_FENCE.search(text or "")
        if fenced and fenced.group(1).strip():
            return WriterAction("sql", fenced.group(1).strip(), "fenced fallback")
        return WriterAction("invalid", None, "reply was not valid JSON and had no ```sql block")

    if not isinstance(data, dict):
        return WriterAction("invalid", None, "reply JSON was not an object")
    action = data.get("action")
    if action == "sql":
        sql = data.get("sql")
        if isinstance(sql, str) and sql.strip():
            return WriterAction("sql", sql.strip(), "")
        return WriterAction("invalid", None, 'action "sql" had no sql string')
    if action == "get_schema":
        return WriterAction("get_schema", None, "")
    if action == "refuse":
        return WriterAction("refuse", None, str(data.get("reason") or "no reason given"))
    return WriterAction("invalid", None, f'unknown or missing "action": {action!r}')


def explainer_prompt(question: str, sql: str, result: ResultSet, max_rows: int = 50) -> str:
    """The explainer's user message: the question, the SQL, and up to ``max_rows`` result rows."""
    shown = result.rows[:max_rows]
    lines = [" | ".join(result.column_names)]
    lines += [" | ".join("NULL" if v is None else str(v) for v in row) for row in shown]
    note = f" (first {len(shown)} shown)" if len(shown) < len(result.rows) else ""
    return (
        f"QUESTION:\n{question.strip()}\n\n"
        f"SQL:\n{sql.strip()}\n\n"
        f"ROW COUNT: {len(result.rows)}{note}\n"
        "ROWS:\n" + "\n".join(lines) + "\n"
    )
