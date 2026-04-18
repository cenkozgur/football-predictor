"""Value engine: edge calculation, fractional Kelly sizing, and banko / kombine tagging.

The rules here are deliberately conservative and match the project's thesis:
we chase positive expected value (EV), not accuracy, and we size stakes so that
a mis-calibrated model cannot destroy the bankroll.

Classification:
    banko_value    — model_prob >= 0.70  AND  edge >= 0.03  (single bets)
    kombine_value  — 0.35 <= model_prob < 0.70  AND  edge >= 0.05  (coupon legs)
    no_value       — otherwise
"""

from __future__ import annotations

from typing import Literal

Tag = Literal["banko_value", "kombine_value", "no_value"]

BANKO_MIN_PROB = 0.70
BANKO_MIN_EDGE = 0.03

KOMBINE_MIN_PROB = 0.35
KOMBINE_MAX_PROB = 0.70
KOMBINE_MIN_EDGE = 0.05


def edge(model_prob: float, decimal_odds: float) -> float:
    """Expected value per 1 unit stake. Positive means +EV."""
    return model_prob * decimal_odds - 1.0


def classify_selection(model_prob: float, edge_value: float) -> Tag:
    if model_prob >= BANKO_MIN_PROB and edge_value >= BANKO_MIN_EDGE:
        return "banko_value"
    if KOMBINE_MIN_PROB <= model_prob < KOMBINE_MAX_PROB and edge_value >= KOMBINE_MIN_EDGE:
        return "kombine_value"
    return "no_value"


def kelly_fraction(model_prob: float, decimal_odds: float) -> float:
    """Full Kelly fraction of bankroll. Returns 0 if bet has no edge."""
    b = decimal_odds - 1.0
    q = 1.0 - model_prob
    if b <= 0:
        return 0.0
    f = (b * model_prob - q) / b
    return max(0.0, f)


def kelly_stake(
    model_prob: float,
    decimal_odds: float,
    bankroll: float,
    fraction: float = 0.25,
) -> float:
    """Fractional Kelly stake in currency units.

    `fraction` is typically 0.25 (quarter Kelly), which empirically survives
    model miscalibration far better than full Kelly.
    """
    f = kelly_fraction(model_prob, decimal_odds)
    return round(bankroll * fraction * f, 2)
