"""Apply context-aware adjustments to Dixon-Coles lambda/mu before deriving markets.

The Dixon-Coles fit gives us a "neutral" expected-goal pair (λ, μ) for any
match. This module layers on per-team motivation into a multiplier — so a
team fighting relegation bumps its attack a little, a dead-rubber team dulls
its effort a little. The adjustments are deliberately *small* because we are
speaking to effort and intent, not ability.

Keep this honest:
  * Multipliers clamp inside ±10% so we never turn the model into a random
    number generator. The signal is directional, not magnitude-heavy.
  * Dead-rubber cuts both attack AND defense since motivation collapse
    shows up as less pressing, sloppier organization — not just fewer goals.
  * Relegation risk and title push boost attack more than they dent defense,
    because desperate teams chase results.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import poisson

from app.features.motivation import TeamMotivation


# How much each stake can move the home/away attack rate, in units of
# multiplicative change (e.g. 0.08 = ±8%). Tuned conservatively — the team-
# strength fit already carries most of the signal.
_RELEGATION_ATTACK = 0.08
_TITLE_ATTACK = 0.06
_EUROPE_ATTACK = 0.04
_DEAD_RUBBER_BOTH_SIDES = 0.10  # dead teams play worse on both ends


@dataclass
class AdjustedRates:
    base_lambda: float      # home λ before adjustment
    base_mu: float          # away μ before adjustment
    lam: float              # adjusted
    mu: float               # adjusted
    home_multiplier: float
    away_multiplier: float
    home_motivation: TeamMotivation | None
    away_motivation: TeamMotivation | None
    reasons: list[str]      # combined, annotated "Ev: ..." / "Deplasman: ..."


def adjust_rates(
    base_lambda: float,
    base_mu: float,
    home: TeamMotivation | None,
    away: TeamMotivation | None,
) -> AdjustedRates:
    """Apply motivation-based multipliers to (λ, μ). Never changes rho or τ."""
    home_mult, home_reasons = _team_multiplier(home, side="home")
    away_mult, away_reasons = _team_multiplier(away, side="away")

    # Home team motivation affects its own attack (λ). If the *away* team is
    # dead-rubber we also lift λ a touch (weaker defense). Symmetric for μ.
    lam_adj = base_lambda * home_mult
    mu_adj = base_mu * away_mult

    if away and away.dead_rubber > 0:
        lam_adj *= 1 + _DEAD_RUBBER_BOTH_SIDES * away.dead_rubber * 0.5
    if home and home.dead_rubber > 0:
        mu_adj *= 1 + _DEAD_RUBBER_BOTH_SIDES * home.dead_rubber * 0.5

    reasons = []
    for r in home_reasons:
        reasons.append(f"Ev: {r}")
    for r in away_reasons:
        reasons.append(f"Dep: {r}")

    return AdjustedRates(
        base_lambda=base_lambda,
        base_mu=base_mu,
        lam=lam_adj,
        mu=mu_adj,
        home_multiplier=home_mult,
        away_multiplier=away_mult,
        home_motivation=home,
        away_motivation=away,
        reasons=reasons,
    )


def _team_multiplier(m: TeamMotivation | None, side: str) -> tuple[float, list[str]]:
    if m is None:
        return 1.0, []
    mult = 1.0
    if m.relegation_risk > 0:
        mult *= 1 + _RELEGATION_ATTACK * m.relegation_risk
    if m.title_push > 0:
        mult *= 1 + _TITLE_ATTACK * m.title_push
    if m.europe_push > 0:
        mult *= 1 + _EUROPE_ATTACK * m.europe_push
    if m.dead_rubber > 0:
        mult *= 1 - _DEAD_RUBBER_BOTH_SIDES * m.dead_rubber
    # Clamp to sane range to defend against pathological cases.
    mult = float(np.clip(mult, 0.85, 1.15))
    return mult, list(m.reasons)


def score_matrix_from_rates(
    lam: float,
    mu: float,
    rho: float,
    max_goals: int = 10,
) -> np.ndarray:
    """Mirror of DixonColesModel.score_matrix but for an arbitrary (λ, μ).

    Kept here rather than in dixon_coles.py so the model class stays
    focused on fitting/storing params; adjustment logic is a separate concern.
    """
    i = np.arange(max_goals + 1)
    home_pmf = poisson.pmf(i, lam)
    away_pmf = poisson.pmf(i, mu)
    matrix = np.outer(home_pmf, away_pmf)
    matrix[0, 0] *= max(1.0 - lam * mu * rho, 1e-10)
    matrix[0, 1] *= max(1.0 + lam * rho, 1e-10)
    matrix[1, 0] *= max(1.0 + mu * rho, 1e-10)
    matrix[1, 1] *= max(1.0 - rho, 1e-10)
    matrix /= matrix.sum()
    return matrix
