from __future__ import annotations

import math

import pytest

from analyst.stats import discordant, mcnemar_exact, paired_bootstrap_diff


def test_mcnemar_known_values() -> None:
    assert mcnemar_exact(0, 6) == pytest.approx(0.03125)
    assert mcnemar_exact(6, 0) == pytest.approx(0.03125)
    assert mcnemar_exact(1, 1) == 1.0
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(0, 1) == 1.0
    assert mcnemar_exact(2, 8) == pytest.approx(0.109375)  # 2 * (1 + 10 + 45) / 1024
    assert mcnemar_exact(0, 10) == pytest.approx(2 / 1024)


def test_mcnemar_is_symmetric_and_a_probability() -> None:
    for b in range(8):
        for c in range(8):
            p = mcnemar_exact(b, c)
            assert 0 < p <= 1
            assert p == mcnemar_exact(c, b)


def test_mcnemar_rejects_negative() -> None:
    with pytest.raises(ValueError):
        mcnemar_exact(-1, 2)


def test_discordant() -> None:
    x = [True, True, False, False, True]
    y = [True, False, True, False, False]
    assert discordant(x, y) == (2, 1)
    assert discordant(y, x) == (1, 2)
    with pytest.raises(ValueError):
        discordant([True], [True, False])


def test_bootstrap_point_estimate_and_determinism() -> None:
    x = [False] * 6 + [True] * 4
    y = [True] * 10
    a = paired_bootstrap_diff(x, y, n_boot=2000, seed=1)
    b = paired_bootstrap_diff(x, y, n_boot=2000, seed=1)
    assert a == b
    assert a[0] == pytest.approx(0.6)
    assert a[1] <= a[0] <= a[2]
    assert a[1] > 0  # 6 of 10 questions flipped to right, none to wrong


def test_bootstrap_identical_arms_have_a_zero_interval() -> None:
    x = [True, False, True, True]
    assert paired_bootstrap_diff(x, x) == (0.0, 0.0, 0.0)


def test_bootstrap_interval_contains_zero_for_a_balanced_split() -> None:
    x = [True, False] * 10
    y = [False, True] * 10
    diff, lo, hi = paired_bootstrap_diff(x, y, seed=3)
    assert diff == 0.0
    assert lo < 0 < hi
    assert math.isfinite(lo)
    assert math.isfinite(hi)


def test_bootstrap_validates_input() -> None:
    with pytest.raises(ValueError):
        paired_bootstrap_diff([], [])
    with pytest.raises(ValueError):
        paired_bootstrap_diff([True], [True, False])
