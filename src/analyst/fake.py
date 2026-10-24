"""Scripted providers for tests and demos.

Fakes exist so the loop, the CLI and the evaluator can be exercised with no model and no
network. They never produce a published number: any result that came from a fake is a plumbing
check, not a measurement of a model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping

from ._vendor.providers import LLMResponse, ProviderError
from .models import GoldenQuestion
from .roles import EXPLAINER_SYSTEM, question_from_prompt


def _response(prompt: str, text: str) -> LLMResponse:
    """A response with rough token counts (4 characters per token) so cost sums are exercised."""
    return LLMResponse(
        text=text,
        model="fake",
        provider="fake",
        latency_s=0.0,
        prompt_tokens=max(1, len(prompt) // 4),
        completion_tokens=max(1, len(text) // 4),
    )


def _reply(action: str, **fields: str) -> str:
    return json.dumps({"action": action, **fields})


class ScriptedProvider:
    """Returns the given replies in order and records every prompt it was sent."""

    def __init__(self, replies: Iterable[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-scripted"

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_object: bool = False,
    ) -> LLMResponse:
        self.prompts.append(prompt)
        self.systems.append(system)
        if len(self.prompts) > len(self._replies):
            raise ProviderError("scripted provider has no replies left")
        return _response(prompt, self._replies[len(self.prompts) - 1])


class GoldenFakeProvider:
    """Answers golden questions with their reference SQL, after a set number of broken replies.

    ``faults[qid] = n`` makes the first ``n`` writer calls for that question return SQL that
    parses but fails ``EXPLAIN`` (a hallucinated column), then the reference SQL. Counts are kept
    per question, so one provider can serve a whole run; `reset` clears them.
    """

    def __init__(
        self, questions: Iterable[GoldenQuestion], faults: Mapping[str, int] | None = None
    ) -> None:
        self._by_question = {q.question.strip(): q for q in questions}
        self._faults = dict(faults or {})
        self._calls: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-golden"

    def reset(self) -> None:
        self._calls.clear()

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_object: bool = False,
    ) -> LLMResponse:
        if system == EXPLAINER_SYSTEM:
            match = re.search(r"ROW COUNT: (\d+)", prompt)
            count = int(match.group(1)) if match else 0
            noun = "row" if count == 1 else "rows"
            return _response(prompt, f"The query returned {count} {noun}.")

        golden = self._by_question.get(question_from_prompt(prompt) or "")
        if golden is None:
            return _response(prompt, _reply("refuse", reason="unknown question"))
        seen = self._calls.get(golden.qid, 0)
        self._calls[golden.qid] = seen + 1
        sql = golden.reference_sql.strip().rstrip(";")
        if seen < self._faults.get(golden.qid, 0):
            sql = f"SELECT no_such_column FROM (\n{sql}\n) AS t"
        return _response(prompt, _reply("sql", sql=sql))


def demo_provider() -> ScriptedProvider:
    """For `ask --fake`: one misspelled column that EXPLAIN catches, a repair, then the answer."""
    broken = "SELECT segment, COUNT(customer_nme) AS customers FROM customers GROUP BY segment"
    fixed = "SELECT segment, COUNT(*) AS customers FROM customers GROUP BY segment ORDER BY segment"
    return ScriptedProvider(
        [
            _reply("sql", sql=broken),
            _reply("sql", sql=fixed),
            "There are three customer segments; the result lists how many customers each has.",
        ]
    )
