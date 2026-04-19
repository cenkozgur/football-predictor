"""Coupon suggestion engine.

Given model-derived probabilities for upcoming matches, suggest parlay coupons
(accumulators) that the user can wager on Bilyoner.

This is *confidence-based* selection — we pick the single strongest prediction
per match across all markets, filter by a probability floor, and combine them.

Important: without bookmaker odds we cannot compute edge or expected value.
A high combined probability does NOT imply a positive-EV bet. This engine
only answers "which accumulators are most likely to WIN" — not "most profitable."
Edge-based selection requires a separate Bilyoner odds ingester.

Markets considered per match
----------------------------
    1X2            → picks the favorite (1, X, or 2)
    Double chance  → 1X, 12, X2
    Over/Under     → for each .5 line, picks the stronger side
    BTTS           → yes / no
    Odd/Even       → odd / even
    Correct score  → only if top-1 score exceeds the pick floor
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Human-readable market labels (Turkish) used in the response.
MARKET_LABELS = {
    "1X2": "Maç Sonucu",
    "double_chance": "Çifte Şans",
    "over_under": "Alt/Üst",
    "btts": "KG Var/Yok",
    "odd_even": "Tek/Çift",
    "correct_score": "Kesin Skor",
}

SELECTION_LABELS = {
    "1": "Ev sahibi",
    "X": "Berabere",
    "2": "Deplasman",
    "1X": "1 veya X",
    "12": "1 veya 2",
    "X2": "X veya 2",
    "yes": "Var",
    "no": "Yok",
    "odd": "Tek",
    "even": "Çift",
    "over": "Üst",
    "under": "Alt",
}


@dataclass
class Pick:
    """A single leg of a coupon — one selection on one match."""

    match_id: int
    home_team: str
    away_team: str
    kickoff: str  # ISO 8601
    league: str
    market: str          # e.g. "1X2", "over_under_2.5", "btts"
    market_label: str    # human-readable (Turkish)
    selection: str       # e.g. "1", "over", "yes"
    selection_label: str
    prob: float          # model probability, 0..1

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": self.kickoff,
            "league": self.league,
            "market": self.market,
            "market_label": self.market_label,
            "selection": self.selection,
            "selection_label": self.selection_label,
            "prob": self.prob,
        }


@dataclass
class Coupon:
    """A multi-leg accumulator suggestion."""

    legs: list[Pick]
    combined_prob: float = field(init=False)

    def __post_init__(self) -> None:
        p = 1.0
        for leg in self.legs:
            p *= leg.prob
        self.combined_prob = p

    def to_dict(self) -> dict[str, Any]:
        return {
            "legs": [leg.to_dict() for leg in self.legs],
            "combined_prob": self.combined_prob,
            "num_legs": len(self.legs),
        }


def _best_pick_for_match(
    match_id: int,
    home: str,
    away: str,
    kickoff: str,
    league: str,
    payload: dict[str, Any],
    allowed_markets: set[str] | None = None,
) -> Pick | None:
    """Return the single highest-probability pick across all markets for one match.

    If `allowed_markets` is given, restrict to those markets.
    """
    candidates: list[Pick] = []

    def add(market: str, selection: str, prob: float) -> None:
        # Extract the base market name (strip "_2.5" suffix etc.) for filtering
        base = market.split("_")[0] if "_" in market else market
        if market.startswith("over_under"):
            base = "over_under"
        elif market.startswith("asian_handicap"):
            base = "asian_handicap"
        elif market.startswith("home_over_under"):
            base = "home_over_under"
        elif market.startswith("away_over_under"):
            base = "away_over_under"

        if allowed_markets is not None and base not in allowed_markets:
            return

        candidates.append(
            Pick(
                match_id=match_id,
                home_team=home,
                away_team=away,
                kickoff=kickoff,
                league=league,
                market=market,
                market_label=MARKET_LABELS.get(base, base),
                selection=selection,
                selection_label=SELECTION_LABELS.get(selection, selection),
                prob=prob,
            )
        )

    # 1X2
    if "1X2" in payload:
        for sel, p in payload["1X2"].items():
            add("1X2", sel, float(p))

    # Double chance
    if "double_chance" in payload:
        for sel, p in payload["double_chance"].items():
            add("double_chance", sel, float(p))

    # Over/Under (all lines). Override the generic Üst/Alt label with the
    # specific line so "0.5 Üst" is visually distinct from "2.5 Üst".
    if "over_under" in payload:
        for line, ou in payload["over_under"].items():
            for sel, p in ou.items():
                pick = Pick(
                    match_id=match_id,
                    home_team=home,
                    away_team=away,
                    kickoff=kickoff,
                    league=league,
                    market=f"over_under_{line}",
                    market_label=MARKET_LABELS["over_under"],
                    selection=sel,
                    selection_label=f"{line} {SELECTION_LABELS.get(sel, sel)}",
                    prob=float(p),
                )
                if allowed_markets is None or "over_under" in allowed_markets:
                    candidates.append(pick)

    # BTTS
    if "btts" in payload:
        for sel, p in payload["btts"].items():
            add("btts", sel, float(p))

    # Odd/Even
    if "odd_even" in payload:
        for sel, p in payload["odd_even"].items():
            add("odd_even", sel, float(p))

    # Correct score — only if top-1 is above threshold; labels differ
    if "correct_score_top10" in payload and payload["correct_score_top10"]:
        top = payload["correct_score_top10"][0]
        score, p = next(iter(top.items()))
        # Correct scores typically have low probabilities (~10-15% max) so they
        # won't win out in a confidence-based selector, which is fine. Including
        # for completeness.
        candidates.append(
            Pick(
                match_id=match_id,
                home_team=home,
                away_team=away,
                kickoff=kickoff,
                league=league,
                market="correct_score",
                market_label=MARKET_LABELS["correct_score"],
                selection=score,
                selection_label=f"Skor {score.replace('-', ':')}",
                prob=float(p),
            )
        )

    if not candidates:
        return None
    return max(candidates, key=lambda c: c.prob)


def suggest_coupons(
    match_predictions: list[dict[str, Any]],
    *,
    min_prob_per_leg: float = 0.60,
    num_legs: int = 3,
    allowed_markets: set[str] | None = None,
    max_coupons: int = 5,
) -> dict[str, Any]:
    """Generate coupon suggestions from a list of match predictions.

    Parameters
    ----------
    match_predictions : list of dicts
        Each dict must have: match_id, home_team, away_team, kickoff, league,
        and payload (the full market payload from `build_full_payload`).
    min_prob_per_leg : float
        Minimum model probability for any leg (default 0.60).
    num_legs : int
        Target number of legs per coupon (1-4 typically).
    allowed_markets : set[str] or None
        If given, restrict picks to these base market names (e.g. {"1X2", "btts"}).
        None = consider every market.
    max_coupons : int
        How many alternative coupons to return.

    Returns
    -------
    dict with:
        - primary: the single strongest N-leg coupon (if num_legs candidates exist)
        - alternatives: up to `max_coupons - 1` other multi-leg combinations
        - bankos: highest-confidence single picks (banko = one-leg sure bets)
        - all_picks: every qualifying pick, sorted by prob desc
    """
    # 1) Compute the single best pick per match
    best_picks: list[Pick] = []
    for m in match_predictions:
        pick = _best_pick_for_match(
            match_id=m["match_id"],
            home=m["home_team"],
            away=m["away_team"],
            kickoff=m["kickoff"],
            league=m["league"],
            payload=m["payload"],
            allowed_markets=allowed_markets,
        )
        if pick is None:
            continue
        if pick.prob >= min_prob_per_leg:
            best_picks.append(pick)

    # Sort strongest first
    best_picks.sort(key=lambda p: p.prob, reverse=True)

    # 2) Build primary coupon: top-N highest-confidence legs
    primary: Coupon | None = None
    if len(best_picks) >= num_legs:
        primary = Coupon(legs=best_picks[:num_legs])

    # 3) Alternative coupons: shifted windows — (1..N+1), (2..N+2), etc.
    #    These represent "next-most-confident" combinations for variety.
    alternatives: list[Coupon] = []
    if len(best_picks) >= num_legs + 1:
        for start in range(1, min(max_coupons, len(best_picks) - num_legs + 1)):
            alternatives.append(Coupon(legs=best_picks[start : start + num_legs]))

    # 4) Bankos — top 5 single picks (all single-leg coupons)
    bankos = [Coupon(legs=[p]) for p in best_picks[:5]]

    return {
        "primary": primary.to_dict() if primary else None,
        "alternatives": [c.to_dict() for c in alternatives],
        "bankos": [c.to_dict() for c in bankos],
        "all_picks": [p.to_dict() for p in best_picks],
        "filters": {
            "min_prob_per_leg": min_prob_per_leg,
            "num_legs": num_legs,
            "allowed_markets": sorted(allowed_markets) if allowed_markets else None,
        },
    }
