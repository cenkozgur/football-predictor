"""Dixon-Coles bivariate Poisson model for football match scores.

Reference: Dixon & Coles (1997), "Modelling Association Football Scores and Inefficiencies
in the Football Betting Market."

Single-league form
------------------
Each team has an attack strength (alpha) and defence strength (beta), with a
global home advantage (gamma):

    lambda = exp(alpha_h + beta_a + gamma)       # home expected goals
    mu     = exp(alpha_a + beta_h)               # away expected goals

The joint score-line probability applies a low-score correction `tau` (rho).

Multi-league pooled form (this module)
--------------------------------------
When fitting on matches from several leagues simultaneously we extend it with
per-league parameters: a per-league home advantage `gamma_L` and a per-league
baseline `delta_L` that captures league-wide scoring rate (e.g. Bundesliga
runs hot, Serie A runs cold). Per-team alpha/beta and the global rho stay
shared across leagues — pooling helps because rho and the optimizer's view
of parameter scale are stabilized by ~5x more matches.

    lambda = exp(alpha_h + beta_a + gamma_L + delta_L)
    mu     = exp(alpha_a + beta_h          + delta_L)

L2 regularization
-----------------
We add an optional ridge penalty `l2 * (sum(alpha^2) + sum(beta^2))` to the
NLL. This is the structural fix for the "overconfident heavy favourite"
failure mode the backtest exposed: it pulls extreme team strengths toward
zero unless the data overwhelmingly demands them.

Identifiability
---------------
Adding any constant `c` to every alpha in league L is absorbed by `delta_L`
(it appears in both lambda and mu). Same for beta. After fitting we therefore
center alpha and beta to mean 0 *within each league* and roll the means into
that league's delta. With L2 active this is a no-op (the penalty already
fixes the gauge), but we apply it unconditionally for consistency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson


_DEFAULT_LEAGUE = "__default__"


def _tau(home_goals: int, away_goals: int, lam: float, mu: float, rho: float) -> float:
    if home_goals == 0 and away_goals == 0:
        return 1.0 - lam * mu * rho
    if home_goals == 0 and away_goals == 1:
        return 1.0 + lam * rho
    if home_goals == 1 and away_goals == 0:
        return 1.0 + mu * rho
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


@dataclass
class DixonColesModel:
    """Fitted Dixon-Coles parameters (single- or multi-league)."""

    teams: list[str] = field(default_factory=list)
    attack: dict[str, float] = field(default_factory=dict)
    defense: dict[str, float] = field(default_factory=dict)
    rho: float = 0.0

    # Multi-league fields. For single-league fits a synthetic league key is used.
    leagues: list[str] = field(default_factory=list)
    home_advs: dict[str, float] = field(default_factory=dict)         # league -> gamma
    league_baselines: dict[str, float] = field(default_factory=dict)  # league -> delta
    team_league: dict[str, str] = field(default_factory=dict)         # team -> league

    @property
    def home_adv(self) -> float:
        """Backwards-compat: scalar home advantage for single-league models."""
        if len(self.leagues) == 1:
            return self.home_advs[self.leagues[0]]
        raise ValueError("home_adv is ambiguous for multi-league models; use home_advs[league]")

    # ---------- fitting ----------

    @classmethod
    def fit(
        cls,
        matches: pd.DataFrame,
        xi: float = 0.0,
        l2: float = 0.0,
        max_goals: int = 10,
    ) -> "DixonColesModel":
        """Fit the model on a dataframe with columns:
        home_team, away_team, home_goals, away_goals, [days_ago], [league].

        Parameters
        ----------
        matches : pd.DataFrame
            Historical match results. If a `league` column is present, fits in
            multi-league pooled mode with per-league gamma/delta. Otherwise
            everything is treated as one synthetic league for backwards compat.
        xi : float
            Time-decay parameter. 0 means no decay. Common values: 0.001 - 0.005 per day.
        l2 : float
            Ridge penalty strength on alpha and beta. 0 disables. Larger values
            shrink team strengths toward zero — use this to combat tail
            overconfidence on small training sets.
        max_goals : int
            Max goals per side considered when building score matrices.
        """
        required = {"home_team", "away_team", "home_goals", "away_goals"}
        missing = required - set(matches.columns)
        if missing:
            raise ValueError(f"matches is missing columns: {missing}")

        # League column is optional. Single-league callers don't need to know it exists.
        if "league" in matches.columns:
            league_col = matches["league"].astype(str)
        else:
            league_col = pd.Series([_DEFAULT_LEAGUE] * len(matches), index=matches.index)

        # Each team belongs to exactly one league (real fixtures never cross).
        team_to_league: dict[str, str] = {}
        for t, lg in zip(matches["home_team"].astype(str), league_col):
            team_to_league.setdefault(t, lg)
        for t, lg in zip(matches["away_team"].astype(str), league_col):
            team_to_league.setdefault(t, lg)

        teams = sorted(team_to_league.keys())
        leagues = sorted(set(team_to_league.values()))
        n = len(teams)
        nl = len(leagues)
        team_idx = {t: i for i, t in enumerate(teams)}
        league_idx = {lg: i for i, lg in enumerate(leagues)}

        if "days_ago" in matches.columns and xi > 0:
            weights = np.exp(-xi * matches["days_ago"].to_numpy(dtype=float))
        else:
            weights = np.ones(len(matches))

        home_ix = matches["home_team"].astype(str).map(team_idx).to_numpy()
        away_ix = matches["away_team"].astype(str).map(team_idx).to_numpy()
        league_ix = league_col.map(league_idx).to_numpy()
        # Goals may be float (when callers pass xG as the goal signal). We keep
        # them float and use a continuous Poisson log-likelihood below.
        home_goals = matches["home_goals"].to_numpy(dtype=float)
        away_goals = matches["away_goals"].to_numpy(dtype=float)

        # Parameter vector: [alpha(n), beta(n), gamma(nl), delta(nl), rho]
        x0 = np.concatenate(
            [
                np.zeros(n),            # alpha
                np.zeros(n),            # beta
                np.full(nl, 0.25),      # gamma per league
                np.zeros(nl),           # delta per league
                np.array([-0.1]),       # rho
            ]
        )

        log_fact_h = _log_factorial(home_goals)
        log_fact_a = _log_factorial(away_goals)
        # Dixon-Coles τ correction only makes sense for discrete integer 0/1
        # goals. When training on xG (non-integer rates) we skip it.
        _is_int = (np.mod(home_goals, 1) == 0) & (np.mod(away_goals, 1) == 0)
        low_score_mask = _is_int & (home_goals <= 1) & (away_goals <= 1)
        low_score_idx = np.where(low_score_mask)[0]
        home_goals_int = home_goals.astype(int)
        away_goals_int = away_goals.astype(int)

        def neg_log_likelihood(params: np.ndarray) -> float:
            alpha = params[:n]
            beta = params[n : 2 * n]
            gamma = params[2 * n : 2 * n + nl]
            delta = params[2 * n + nl : 2 * n + 2 * nl]
            rho = params[2 * n + 2 * nl]

            lam = np.exp(alpha[home_ix] + beta[away_ix] + gamma[league_ix] + delta[league_ix])
            mu = np.exp(alpha[away_ix] + beta[home_ix] + delta[league_ix])

            log_lik = (
                home_goals * np.log(lam) - lam - log_fact_h
                + away_goals * np.log(mu) - mu - log_fact_a
            )

            # Dixon-Coles low-score correction (only the 0/1 cells need it)
            if low_score_idx.size:
                tau = np.ones_like(log_lik)
                for k in low_score_idx:
                    t = _tau(
                        int(home_goals_int[k]),
                        int(away_goals_int[k]),
                        float(lam[k]),
                        float(mu[k]),
                        float(rho),
                    )
                    tau[k] = max(t, 1e-10)
                log_lik = log_lik + np.log(tau)

            nll = -float(np.sum(weights * log_lik))
            if l2 > 0:
                nll += l2 * float(np.sum(alpha * alpha) + np.sum(beta * beta))
            return nll

        bounds: list[tuple[float | None, float | None]] = (
            [(None, None)] * (2 * n)
            + [(-1.0, 1.5)] * nl
            + [(-2.0, 2.0)] * nl
            + [(-0.5, 0.5)]
        )

        result = minimize(
            neg_log_likelihood,
            x0,
            method="L-BFGS-B",
            bounds=bounds,
            options={"maxiter": 500, "ftol": 1e-8},
        )

        params = result.x
        alpha = params[:n].copy()
        beta = params[n : 2 * n].copy()
        gamma = params[2 * n : 2 * n + nl].copy()
        delta = params[2 * n + nl : 2 * n + 2 * nl].copy()
        rho = float(params[2 * n + 2 * nl])

        # Identifiability fix: center alpha and beta within each league, roll
        # the means into that league's delta. (Adding c to every alpha_L and
        # subtracting c from delta_L is a free symmetry of the likelihood.)
        for lg in leagues:
            l_idx = league_idx[lg]
            team_indices = [team_idx[t] for t in teams if team_to_league[t] == lg]
            if not team_indices:
                continue
            a_mean = float(alpha[team_indices].mean())
            b_mean = float(beta[team_indices].mean())
            for ti in team_indices:
                alpha[ti] -= a_mean
                beta[ti] -= b_mean
            delta[l_idx] += a_mean + b_mean

        return cls(
            teams=teams,
            attack={t: float(alpha[team_idx[t]]) for t in teams},
            defense={t: float(beta[team_idx[t]]) for t in teams},
            rho=rho,
            leagues=leagues,
            home_advs={lg: float(gamma[league_idx[lg]]) for lg in leagues},
            league_baselines={lg: float(delta[league_idx[lg]]) for lg in leagues},
            team_league=dict(team_to_league),
        )

    # ---------- prediction ----------

    def _league_of(self, home_team: str) -> str:
        lg = self.team_league.get(home_team)
        if lg is not None:
            return lg
        if len(self.leagues) == 1:
            return self.leagues[0]
        raise KeyError(f"No league mapping for team: {home_team}")

    def rates(self, home_team: str, away_team: str) -> tuple[float, float]:
        """Expected goals (lambda, mu) for a scheduled match."""
        if home_team not in self.attack or away_team not in self.attack:
            raise KeyError(f"Unknown team: {home_team} or {away_team}")
        league = self._league_of(home_team)
        gamma = self.home_advs[league]
        delta = self.league_baselines[league]
        lam = float(np.exp(self.attack[home_team] + self.defense[away_team] + gamma + delta))
        mu = float(np.exp(self.attack[away_team] + self.defense[home_team] + delta))
        return lam, mu

    def score_matrix(self, home_team: str, away_team: str, max_goals: int = 10) -> np.ndarray:
        """Joint probability matrix P(home_goals=i, away_goals=j), shape (max_goals+1, max_goals+1)."""
        lam, mu = self.rates(home_team, away_team)
        i = np.arange(max_goals + 1)
        home_pmf = poisson.pmf(i, lam)
        away_pmf = poisson.pmf(i, mu)
        matrix = np.outer(home_pmf, away_pmf)

        matrix[0, 0] *= max(1.0 - lam * mu * self.rho, 1e-10)
        matrix[0, 1] *= max(1.0 + lam * self.rho, 1e-10)
        matrix[1, 0] *= max(1.0 + mu * self.rho, 1e-10)
        matrix[1, 1] *= max(1.0 - self.rho, 1e-10)

        matrix /= matrix.sum()
        return matrix


def _log_factorial(x: np.ndarray) -> np.ndarray:
    """Vectorized log factorial via gammaln."""
    from scipy.special import gammaln

    return gammaln(x + 1)
