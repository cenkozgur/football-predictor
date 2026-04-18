"""Verify that markets derivation is internally consistent.

Every market derived from the same score matrix must satisfy basic probability axioms.
If these pass, every downstream market on every match is mathematically sound.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import poisson

from app.ml.markets import (
    asian_handicap,
    btts,
    double_chance,
    goal_range,
    odd_even,
    one_x_two,
    over_under,
    team_over_under,
)
from app.ml.value import classify_selection, edge, kelly_fraction, kelly_stake


@pytest.fixture
def score_matrix() -> np.ndarray:
    """A realistic independent-Poisson matrix for lambda=1.6, mu=1.1 (home slightly favored)."""
    max_goals = 10
    i = np.arange(max_goals + 1)
    home = poisson.pmf(i, 1.6)
    away = poisson.pmf(i, 1.1)
    m = np.outer(home, away)
    return m / m.sum()


def test_score_matrix_is_normalized(score_matrix: np.ndarray) -> None:
    assert np.isclose(score_matrix.sum(), 1.0)


def test_one_x_two_sums_to_one(score_matrix: np.ndarray) -> None:
    r = one_x_two(score_matrix)
    assert set(r) == {"1", "X", "2"}
    assert np.isclose(sum(r.values()), 1.0, atol=1e-6)


def test_home_favored(score_matrix: np.ndarray) -> None:
    r = one_x_two(score_matrix)
    # lambda > mu, so home should win more often than away
    assert r["1"] > r["2"]


def test_double_chance_consistent(score_matrix: np.ndarray) -> None:
    r = one_x_two(score_matrix)
    dc = double_chance(score_matrix)
    assert np.isclose(dc["1X"], r["1"] + r["X"], atol=1e-9)
    assert np.isclose(dc["12"], r["1"] + r["2"], atol=1e-9)
    assert np.isclose(dc["X2"], r["X"] + r["2"], atol=1e-9)


def test_over_under_half_line_sums_to_one(score_matrix: np.ndarray) -> None:
    for line in (0.5, 1.5, 2.5, 3.5, 4.5):
        r = over_under(score_matrix, line)
        assert np.isclose(r["over"] + r["under"], 1.0, atol=1e-6), f"line={line}"


def test_over_under_monotone_in_line(score_matrix: np.ndarray) -> None:
    p_over_15 = over_under(score_matrix, 1.5)["over"]
    p_over_25 = over_under(score_matrix, 2.5)["over"]
    p_over_35 = over_under(score_matrix, 3.5)["over"]
    assert p_over_15 > p_over_25 > p_over_35


def test_btts_sums_to_one(score_matrix: np.ndarray) -> None:
    r = btts(score_matrix)
    assert np.isclose(r["yes"] + r["no"], 1.0, atol=1e-9)


def test_btts_definition(score_matrix: np.ndarray) -> None:
    """P(BTTS yes) == 1 - P(home=0) - P(away=0) + P(0-0)."""
    p_home_zero = score_matrix[0, :].sum()
    p_away_zero = score_matrix[:, 0].sum()
    p_zero_zero = score_matrix[0, 0]
    expected = 1.0 - p_home_zero - p_away_zero + p_zero_zero
    assert np.isclose(btts(score_matrix)["yes"], expected, atol=1e-9)


def test_goal_range_sums_to_one(score_matrix: np.ndarray) -> None:
    r = goal_range(score_matrix)
    assert np.isclose(sum(r.values()), 1.0, atol=1e-6)


def test_odd_even_sums_to_one(score_matrix: np.ndarray) -> None:
    r = odd_even(score_matrix)
    assert np.isclose(r["odd"] + r["even"], 1.0, atol=1e-9)


def test_team_over_under_sums_to_one(score_matrix: np.ndarray) -> None:
    for side in ("home", "away"):
        for line in (0.5, 1.5, 2.5):
            r = team_over_under(score_matrix, line, side)
            assert np.isclose(r["over"] + r["under"], 1.0, atol=1e-6)


def test_asian_handicap_zero_equals_1x2_minus_draw(score_matrix: np.ndarray) -> None:
    """AH 0 splits draws: home gets 0.5 * P(draw), away gets 0.5 * P(draw)."""
    r = one_x_two(score_matrix)
    ah = asian_handicap(score_matrix, 0.0)
    assert np.isclose(ah["home"], r["1"] + 0.5 * r["X"], atol=1e-9)
    assert np.isclose(ah["away"], r["2"] + 0.5 * r["X"], atol=1e-9)


def test_asian_handicap_half_line_sums_to_one(score_matrix: np.ndarray) -> None:
    for line in (-1.5, -0.5, 0.5, 1.5):
        ah = asian_handicap(score_matrix, line)
        assert np.isclose(ah["home"] + ah["away"], 1.0, atol=1e-6), f"line={line}"


def test_asian_handicap_quarter_line(score_matrix: np.ndarray) -> None:
    """AH -0.25 should be the average of AH 0 and AH -0.5."""
    ah0 = asian_handicap(score_matrix, 0.0)
    ah_half = asian_handicap(score_matrix, -0.5)
    ah_quarter = asian_handicap(score_matrix, -0.25)
    assert np.isclose(ah_quarter["home"], 0.5 * (ah0["home"] + ah_half["home"]), atol=1e-9)


# ---------- value engine ----------


def test_edge_definition() -> None:
    assert np.isclose(edge(0.5, 2.2), 0.10)
    assert edge(0.5, 2.0) == 0.0
    assert edge(0.4, 2.0) < 0


def test_classify_banko() -> None:
    # prob 0.75, odds 1.50 => edge = 0.125 >= 0.03 and prob >= 0.70
    assert classify_selection(0.75, 0.125) == "banko_value"


def test_classify_kombine() -> None:
    # prob 0.40, odds 3.00 => edge = 0.20
    assert classify_selection(0.40, 0.20) == "kombine_value"


def test_classify_no_value() -> None:
    assert classify_selection(0.55, 0.02) == "no_value"  # edge too small for its bucket
    assert classify_selection(0.75, 0.01) == "no_value"  # banko edge too small


def test_kelly_fraction_positive_edge() -> None:
    # Fair 50/50 bet at 2.20 odds => full Kelly = (1.2 * 0.5 - 0.5) / 1.2 ≈ 0.0833
    f = kelly_fraction(0.5, 2.2)
    assert np.isclose(f, (1.2 * 0.5 - 0.5) / 1.2, atol=1e-9)


def test_kelly_fraction_zero_for_negative_edge() -> None:
    assert kelly_fraction(0.4, 2.0) == 0.0


def test_kelly_stake_quarter_kelly() -> None:
    # Bankroll 1000, model 0.6, odds 2.0 => full Kelly = (1*0.6 - 0.4)/1 = 0.2
    # ¼ Kelly stake = 1000 * 0.25 * 0.2 = 50
    assert kelly_stake(0.6, 2.0, bankroll=1000.0, fraction=0.25) == 50.0
