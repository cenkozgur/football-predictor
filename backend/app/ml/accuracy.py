"""Prediction accuracy evaluation.

Given finished matches with predictions, compute:
  • per-market hit rate (model's top pick vs actual outcome)
  • calibration bins (when model says 70%, does it really hit 70%?)
  • a per-prediction detail list for recent rows

"Model's pick" = argmax of the market's probability map. Only markets present
in the payload are scored — missing markets are simply skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass
class EvalRow:
    match_id: int
    kickoff: str
    league: str
    home_team: str
    away_team: str
    ft_home: int
    ft_away: int
    market: str
    pick: str
    pick_prob: float
    actual: str
    hit: bool


def _argmax(d: dict[str, float]) -> tuple[str, float] | None:
    if not d:
        return None
    k = max(d, key=d.get)
    return k, d[k]


def _actual_1x2(ft_home: int, ft_away: int) -> str:
    if ft_home > ft_away:
        return "1"
    if ft_home < ft_away:
        return "2"
    return "X"


def _actual_double_chance(ft_home: int, ft_away: int) -> set[str]:
    r = _actual_1x2(ft_home, ft_away)
    if r == "1":
        return {"1X", "12"}
    if r == "2":
        return {"12", "X2"}
    return {"1X", "X2"}


def _evaluate_one(
    match_id: int,
    kickoff: str,
    league: str,
    home: str,
    away: str,
    ft_home: int,
    ft_away: int,
    payload: dict[str, Any],
) -> list[EvalRow]:
    total = ft_home + ft_away
    rows: list[EvalRow] = []

    # 1X2
    if isinstance(payload.get("1X2"), dict):
        pick = _argmax(payload["1X2"])
        if pick:
            actual = _actual_1x2(ft_home, ft_away)
            rows.append(EvalRow(match_id, kickoff, league, home, away, ft_home, ft_away,
                                "1X2", pick[0], pick[1], actual, pick[0] == actual))

    # Double chance
    if isinstance(payload.get("double_chance"), dict):
        pick = _argmax(payload["double_chance"])
        if pick:
            actuals = _actual_double_chance(ft_home, ft_away)
            rows.append(EvalRow(match_id, kickoff, league, home, away, ft_home, ft_away,
                                "double_chance", pick[0], pick[1],
                                "/".join(sorted(actuals)), pick[0] in actuals))

    # Over/Under — score each line separately (the 2.5 line is the canonical one)
    ou = payload.get("over_under")
    if isinstance(ou, dict):
        for line_str, probs in ou.items():
            if not isinstance(probs, dict):
                continue
            pick = _argmax(probs)
            if not pick:
                continue
            try:
                line = float(line_str)
            except ValueError:
                continue
            actual = "over" if total > line else "under"
            rows.append(EvalRow(match_id, kickoff, league, home, away, ft_home, ft_away,
                                f"over_under_{line_str}", pick[0], pick[1], actual, pick[0] == actual))

    # BTTS
    if isinstance(payload.get("btts"), dict):
        pick = _argmax(payload["btts"])
        if pick:
            actual = "yes" if (ft_home > 0 and ft_away > 0) else "no"
            rows.append(EvalRow(match_id, kickoff, league, home, away, ft_home, ft_away,
                                "btts", pick[0], pick[1], actual, pick[0] == actual))

    # Odd/Even
    if isinstance(payload.get("odd_even"), dict):
        pick = _argmax(payload["odd_even"])
        if pick:
            actual = "odd" if total % 2 == 1 else "even"
            rows.append(EvalRow(match_id, kickoff, league, home, away, ft_home, ft_away,
                                "odd_even", pick[0], pick[1], actual, pick[0] == actual))

    # Correct score (top-1 only)
    cs = payload.get("correct_score_top10")
    if isinstance(cs, list) and cs:
        top = cs[0]
        if isinstance(top, dict) and top:
            score, prob = next(iter(top.items()))
            actual = f"{ft_home}-{ft_away}"
            rows.append(EvalRow(match_id, kickoff, league, home, away, ft_home, ft_away,
                                "correct_score", score, prob, actual, score == actual))

    return rows


def evaluate_predictions(
    items: Iterable[tuple[int, str, str, str, str, int, int, dict[str, Any]]],
) -> list[EvalRow]:
    """Items: (match_id, kickoff_iso, league, home, away, ft_home, ft_away, payload)."""
    out: list[EvalRow] = []
    for it in items:
        out.extend(_evaluate_one(*it))
    return out


def summarize(rows: list[EvalRow]) -> dict[str, Any]:
    """Aggregate hit rate per market and calibration bins (10% wide)."""
    by_market: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = by_market.setdefault(r.market, {"picks": 0, "hits": 0, "prob_sum": 0.0})
        b["picks"] += 1
        b["hits"] += int(r.hit)
        b["prob_sum"] += r.pick_prob

    for m, b in by_market.items():
        n = b["picks"]
        b["hit_rate"] = b["hits"] / n if n else 0.0
        b["avg_prob"] = b["prob_sum"] / n if n else 0.0
        del b["prob_sum"]

    # Calibration bins: 50-59%, 60-69%, 70-79%, 80-89%, 90-100%
    bin_edges = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01)]
    calib = []
    for lo, hi in bin_edges:
        bucket = [r for r in rows if lo <= r.pick_prob < hi]
        n = len(bucket)
        calib.append({
            "range": f"{int(lo*100)}-{int(hi*100 if hi <= 1 else 100)}%",
            "picks": n,
            "hits": sum(1 for r in bucket if r.hit),
            "hit_rate": (sum(1 for r in bucket if r.hit) / n) if n else 0.0,
            "avg_prob": (sum(r.pick_prob for r in bucket) / n) if n else 0.0,
        })

    total_picks = len(rows)
    total_hits = sum(1 for r in rows if r.hit)
    return {
        "overall": {
            "picks": total_picks,
            "hits": total_hits,
            "hit_rate": total_hits / total_picks if total_picks else 0.0,
        },
        "by_market": by_market,
        "calibration": calib,
    }
