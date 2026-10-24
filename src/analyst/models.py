"""The shapes every module agrees on.

One question flows through the loop as a list of `Step`s and ends as a `Trace`. The evaluator
turns a `Trace` plus the golden reference into an `EvalRow`. Nothing here talks to a model or a
database, so these types can be built by hand in tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ._vendor.results import ResultSet

#: How a question ended. Exactly one per trace.
#:
#: - ``ok``: SQL passed validation, ran, and returned a result (correct or not is the scorer's job).
#: - ``failed_validation``: the last attempt still failed the parser/EXPLAIN check.
#: - ``failed_execution``: SQL validated but the engine raised at run time (or hit the timeout).
#: - ``refused_unsafe``: the read-only guard rejected the SQL (a write, DDL, stacked statements).
#: - ``budget_exhausted``: the writer never produced usable SQL within its call budget
#:   (unparseable replies, schema requests only, an explicit refusal).
Outcome = Literal[
    "ok", "failed_validation", "failed_execution", "refused_unsafe", "budget_exhausted"
]
OUTCOMES: tuple[str, ...] = (
    "ok",
    "failed_validation",
    "failed_execution",
    "refused_unsafe",
    "budget_exhausted",
)

#: What a step did. ``writer`` and ``explain`` are LLM calls; the rest are deterministic.
StepKind = Literal["writer", "get_schema", "guard", "validate", "execute", "explain"]
LLM_STEPS = frozenset({"writer", "explain"})

#: Arm name -> maximum writer attempts. Arms differ in this number and nothing else:
#: same model, same prompt, same seed. Arm A gets no second chance, so a validation error ends it.
ARMS: dict[str, int] = {"A": 1, "B": 2, "C": 3}

#: Failure taxonomy for wrong answers, from the brief. ``None`` on an EvalRow means correct.
FAILURE_KINDS: tuple[str, ...] = (
    "syntax",  # never produced SQL that parsed/bound (failed_validation)
    "execution",  # validated, then raised at run time
    "unsafe",  # the guard refused it
    "no_sql",  # budget_exhausted: the writer never gave usable SQL
    "wrong_columns",  # result has a different column count from the reference
    "wrong_grain",  # same columns, different row count
    "wrong_values",  # same shape, different values (wrong filter, join or arithmetic)
    "wrong_order",  # right multiset, wrong order where order matters
)


@dataclass(frozen=True)
class GoldenQuestion:
    """One row of ``golden/questions.jsonl``."""

    qid: str
    question: str
    pattern: str
    reference_sql: str
    order_matters: bool = False
    tags: tuple[str, ...] = ()
    example: bool = False
    #: True when the question fixes the order of the output columns (a pivot, or "in that order"):
    #: the scorer then compares column by column in position, with no permutation.
    column_order_matters: bool = False
    expected_digest: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoldenQuestion:
        return cls(
            qid=str(data["qid"]),
            question=str(data["question"]),
            pattern=str(data["pattern"]),
            reference_sql=str(data["reference_sql"]),
            order_matters=bool(data.get("order_matters", False)),
            tags=tuple(data.get("tags", ())),
            example=bool(data.get("example", False)),
            column_order_matters=bool(data.get("column_order_matters", False)),
            expected_digest=data.get("expected_digest"),
        )

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["tags"] = list(self.tags)
        if not self.column_order_matters:
            # Written only when true, so a file that never used the key keeps its exact shape.
            del out["column_order_matters"]
        return out


@dataclass
class Step:
    """One thing the loop did. ``input``/``output`` are text so a trace prints and diffs cleanly."""

    kind: StepKind
    input: str = ""
    output: str = ""
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    cached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Trace:
    """Everything that happened for one question, in order."""

    question: str
    steps: list[Step] = field(default_factory=list)
    attempts: int = 0
    final_sql: str | None = None
    result: ResultSet | None = None
    explanation: str | None = None
    outcome: Outcome = "budget_exhausted"

    @property
    def llm_calls(self) -> int:
        return sum(1 for s in self.steps if s.kind in LLM_STEPS)

    @property
    def writer_calls(self) -> int:
        return sum(1 for s in self.steps if s.kind == "writer")

    @property
    def prompt_tokens(self) -> int:
        return sum(s.prompt_tokens for s in self.steps)

    @property
    def completion_tokens(self) -> int:
        return sum(s.completion_tokens for s in self.steps)

    @property
    def latency_s(self) -> float:
        return sum(s.latency_s for s in self.steps)

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "outcome": self.outcome,
            "attempts": self.attempts,
            "final_sql": self.final_sql,
            "result": self.result.as_dict() if self.result is not None else None,
            "explanation": self.explanation,
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_s": round(self.latency_s, 6),
            "steps": [s.as_dict() for s in self.steps],
        }


@dataclass(frozen=True)
class EvalRow:
    """One question scored under one arm. A run is a list of these, written as JSONL."""

    qid: str
    arm: str
    attempts: int
    ok: bool
    outcome: str
    failure_kind: str | None
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    pattern: str = ""
    #: True when this row counts as correct only because the agent's columns had to be reordered
    #: to match the reference (never true for a wrong answer).
    column_mapping_used: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalRow:
        return cls(**data)
