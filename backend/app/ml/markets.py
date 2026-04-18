"""Derive every betting market analytically from a joint score probability matrix.

The input to every function is a 2D numpy array `matrix` where
`matrix[i, j] = P(home_goals = i, away_goals = j)`, summing to (approximately) 1.

This is the architectural keystone: one calibrated Dixon-Coles fit unlocks every
market below without a separate model.

Supported markets:
    - 1X2 (match result)
    - Double chance (çifte şans)
    - Over/Under totals (alt/üst), any line with 0.5 step
    - BTTS / KG Var-Yok
    - Correct score (kesin skor), top-k
    - Goal range (0-1, 2-3, 4-6, 7+)
    - Odd / even total goals (tek/çift)
    - Asian handicap (quarter lines too)
    - Home/away team over/under N goals
"""

from __future__ import annotations

from typing import Any

import numpy as np


# ---------- 1X2 and double chance ----------


def one_x_two(matrix: np.ndarray) -> dict[str, float]:
    """Match result probabilities keyed '1', 'X', '2'."""
    n = matrix.shape[0]
    i, j = np.indices((n, n))
    home_win = float(matrix[i > j].sum())
    draw = float(matrix[i == j].sum())
    away_win = float(matrix[i < j].sum())
    return {"1": home_win, "X": draw, "2": away_win}


def double_chance(matrix: np.ndarray) -> dict[str, float]:
    r = one_x_two(matrix)
    return {
        "1X": r["1"] + r["X"],
        "12": r["1"] + r["2"],
        "X2": r["X"] + r["2"],
    }


# ---------- over / under totals ----------


def over_under(matrix: np.ndarray, line: float) -> dict[str, float]:
    """Over/Under total goals at an arbitrary .5 or .25 / .75 line.

    For .5 lines (2.5 etc.) this is a clean two-way market.
    For .0 lines (2.0 etc.) pushes are possible — we split the push into half-over/half-under.
    For quarter lines (2.25, 2.75) we average the two adjacent half-integer lines.
    """
    n = matrix.shape[0]
    i, j = np.indices((n, n))
    totals = i + j

    # Handle quarter lines by averaging
    frac = line - int(line)
    if abs(frac - 0.25) < 1e-9:
        a = over_under(matrix, line - 0.25)
        b = over_under(matrix, line + 0.25)
        return {"over": 0.5 * (a["over"] + b["over"]), "under": 0.5 * (a["under"] + b["under"])}
    if abs(frac - 0.75) < 1e-9:
        a = over_under(matrix, line - 0.25)
        b = over_under(matrix, line + 0.25)
        return {"over": 0.5 * (a["over"] + b["over"]), "under": 0.5 * (a["under"] + b["under"])}

    if abs(frac - 0.5) < 1e-9:
        over = float(matrix[totals > line].sum())
        under = float(matrix[totals < line].sum())
        return {"over": over, "under": under}

    # Whole-number line: push on equality, split evenly
    over = float(matrix[totals > line].sum())
    under = float(matrix[totals < line].sum())
    push = float(matrix[totals == line].sum())
    return {"over": over + 0.5 * push, "under": under + 0.5 * push}


def over_under_standard_lines(matrix: np.ndarray) -> dict[str, dict[str, float]]:
    """Convenience: O/U for 0.5, 1.5, 2.5, 3.5, 4.5."""
    return {str(line): over_under(matrix, line) for line in (0.5, 1.5, 2.5, 3.5, 4.5)}


# ---------- BTTS / KG Var-Yok ----------


def btts(matrix: np.ndarray) -> dict[str, float]:
    n = matrix.shape[0]
    i, j = np.indices((n, n))
    yes = float(matrix[(i >= 1) & (j >= 1)].sum())
    return {"yes": yes, "no": 1.0 - yes}


# ---------- correct score ----------


def correct_score_top_k(matrix: np.ndarray, k: int = 10) -> list[dict[str, float]]:
    """Return the top-k most likely score lines as [{'2-1': 0.11}, ...]."""
    n = matrix.shape[0]
    flat = [(matrix[i, j], f"{i}-{j}") for i in range(n) for j in range(n)]
    flat.sort(reverse=True)
    return [{score: float(p)} for p, score in flat[:k]]


# ---------- goal range ----------


def goal_range(matrix: np.ndarray) -> dict[str, float]:
    """Standard Turkish/European goal-range brackets: 0-1, 2-3, 4-6, 7+."""
    n = matrix.shape[0]
    i, j = np.indices((n, n))
    totals = i + j
    return {
        "0-1": float(matrix[totals <= 1].sum()),
        "2-3": float(matrix[(totals >= 2) & (totals <= 3)].sum()),
        "4-6": float(matrix[(totals >= 4) & (totals <= 6)].sum()),
        "7+": float(matrix[totals >= 7].sum()),
    }


# ---------- tek/çift (odd/even) ----------


def odd_even(matrix: np.ndarray) -> dict[str, float]:
    n = matrix.shape[0]
    i, j = np.indices((n, n))
    totals = i + j
    odd = float(matrix[totals % 2 == 1].sum())
    return {"odd": odd, "even": 1.0 - odd}


# ---------- Asian handicap ----------


def asian_handicap(matrix: np.ndarray, line: float) -> dict[str, float]:
    """Asian handicap where `line` is applied to the HOME team.

    line = -1.5 means home team starts at -1.5 goals.
    line = +0.5 means home team starts at +0.5 goals.

    Quarter lines (.25, .75) are handled by averaging the two adjacent half lines,
    matching how Asian bookmakers settle split-stake bets.
    """
    frac = line - np.floor(line)
    if abs(frac - 0.25) < 1e-9:
        a = asian_handicap(matrix, line - 0.25)
        b = asian_handicap(matrix, line + 0.25)
        return {"home": 0.5 * (a["home"] + b["home"]), "away": 0.5 * (a["away"] + b["away"])}
    if abs(frac - 0.75) < 1e-9:
        a = asian_handicap(matrix, line - 0.25)
        b = asian_handicap(matrix, line + 0.25)
        return {"home": 0.5 * (a["home"] + b["home"]), "away": 0.5 * (a["away"] + b["away"])}

    n = matrix.shape[0]
    i, j = np.indices((n, n))
    diff = i - j + line  # adjusted home margin

    if abs(frac - 0.5) < 1e-9:
        home = float(matrix[diff > 0].sum())
        away = float(matrix[diff < 0].sum())
        return {"home": home, "away": away}

    # Whole-number handicap: push on exact zero, split evenly
    home = float(matrix[diff > 0].sum())
    away = float(matrix[diff < 0].sum())
    push = float(matrix[np.isclose(diff, 0.0)].sum())
    return {"home": home + 0.5 * push, "away": away + 0.5 * push}


# ---------- team totals ----------


def team_over_under(matrix: np.ndarray, line: float, side: str) -> dict[str, float]:
    """Over/Under for a single team's goal count. `side` is 'home' or 'away'."""
    n = matrix.shape[0]
    i, j = np.indices((n, n))
    goals = i if side == "home" else j

    frac = line - int(line)
    if abs(frac - 0.5) < 1e-9:
        over = float(matrix[goals > line].sum())
        under = float(matrix[goals < line].sum())
        return {"over": over, "under": under}

    over = float(matrix[goals > line].sum())
    under = float(matrix[goals < line].sum())
    push = float(matrix[goals == line].sum())
    return {"over": over + 0.5 * push, "under": under + 0.5 * push}


# ---------- full payload ----------


def build_full_payload(matrix: np.ndarray) -> dict[str, Any]:
    """Build the complete prediction payload consumed by the API / frontend."""
    return {
        "1X2": one_x_two(matrix),
        "double_chance": double_chance(matrix),
        "over_under": over_under_standard_lines(matrix),
        "btts": btts(matrix),
        "odd_even": odd_even(matrix),
        "goal_range": goal_range(matrix),
        "correct_score_top10": correct_score_top_k(matrix, k=10),
        "asian_handicap_0": asian_handicap(matrix, 0.0),
        "asian_handicap_-0.5": asian_handicap(matrix, -0.5),
        "asian_handicap_-1": asian_handicap(matrix, -1.0),
        "asian_handicap_+0.5": asian_handicap(matrix, 0.5),
        "asian_handicap_+1": asian_handicap(matrix, 1.0),
        "home_over_under_1.5": team_over_under(matrix, 1.5, "home"),
        "away_over_under_1.5": team_over_under(matrix, 1.5, "away"),
    }
