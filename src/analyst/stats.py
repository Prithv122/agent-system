"""Paired statistics for comparing two arms on the same questions.

Each question is answered under every arm, so the data are paired: what matters is the questions
where the arms disagree (the discordant pairs). An exact McNemar test asks whether those
disagreements lean one way more than chance; a paired bootstrap puts a confidence interval on the
accuracy difference. Standard library only, so the numbers do not depend on a numeric stack.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def discordant(x: Sequence[bool], y: Sequence[bool]) -> tuple[int, int]:
    """(b, c): b = x right and y wrong, c = x wrong and y right."""
    if len(x) != len(y):
        raise ValueError(f"paired samples must have the same length, got {len(x)} and {len(y)}")
    b = sum(1 for a, d in zip(x, y, strict=True) if a and not d)
    c = sum(1 for a, d in zip(x, y, strict=True) if d and not a)
    return b, c


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts.

    Under the null each discordant pair is a fair coin, so ``min(b, c)`` follows Binomial(b + c,
    1/2). The two-sided p doubles the lower tail and is capped at 1. With no discordant pairs there
    is nothing to test and p is 1.
    """
    if b < 0 or c < 0:
        raise ValueError("discordant counts must be non-negative")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1))
    return min(1.0, 2 * tail / 2**n)


def paired_bootstrap_diff(
    x: Sequence[bool],
    y: Sequence[bool],
    *,
    n_boot: int = 10_000,
    seed: int = 20260830,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """(mean(y) - mean(x), lo, hi): a percentile bootstrap interval, resampling questions.

    Question indices are resampled with replacement and both arms keep their paired outcome, so the
    interval reflects question-to-question variation, not just the arms. Deterministic for a seed.
    """
    if len(x) != len(y):
        raise ValueError(f"paired samples must have the same length, got {len(x)} and {len(y)}")
    n = len(x)
    if n == 0:
        raise ValueError("cannot bootstrap an empty sample")
    deltas = [int(b) - int(a) for a, b in zip(x, y, strict=True)]
    point = sum(deltas) / n
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(deltas, k=n)) / n for _ in range(n_boot))
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, math.ceil((1 - alpha / 2) * n_boot) - 1)]
    return point, lo, hi
