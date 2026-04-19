"""Coupon suggestion engine — composite-signal selection.

Earlier iteration ranked picks by model probability alone, which meant every
coupon was three legs of 0.5 Üst (mathematically near-certain, payout near-1x).
The user's goal is a *profitable* coupon with explicit reasons — why this
match, not that one. So each pick now carries a composite score built from:

    1. Model probability        (Dixon-Coles + motivation-adjusted Poisson)
    2. Value edge               (model prob vs. bookmaker implied prob)
    3. Motivation alignment     (does the stake support the pick direction?)
    4. Form alignment           (recent results support the pick?)

Each signal contributes a Turkish "neden" string that surfaces in the UI,
so the user sees not "we picked this, trust us" but "we picked this because
X, Y, Z — and discarded the alternative because it had only X."

Coupon composition then greedy-maximizes composite score while enforcing
market diversity (no three Alt/Üst legs) and a minimum combined-odds target
(default 1.6x — below that the coupon is not worth playing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Human-readable market labels (Turkish).
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


# Composite-score weights. Tuned so model probability remains dominant but
# a strong value edge or motivation story can lift a mid-prob pick above
# a prob-only favorite. Must sum to 1.0.
_W_PROB = 0.40
_W_VALUE = 0.30
_W_MOTIVATION = 0.20
_W_FORM = 0.10


@dataclass
class Pick:
    """A single leg of a coupon — one selection on one match, with reasons."""

    match_id: int
    home_team: str
    away_team: str
    kickoff: str
    league: str
    market: str
    market_label: str
    selection: str
    selection_label: str
    prob: float              # model probability, 0..1
    book_odds: float | None  # best available decimal odds, or None
    book_prob: float | None  # implied probability, or None
    value_edge: float        # (model_prob / book_prob) - 1; 0 if unknown
    motivation_score: float  # -1..1; positive = motivation supports the pick
    form_score: float        # -1..1; positive = recent form supports the pick
    composite: float         # weighted blend in [0, 1]
    reasons: list[str] = field(default_factory=list)  # Turkish, UI-facing

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
            "book_odds": self.book_odds,
            "book_prob": self.book_prob,
            "value_edge": self.value_edge,
            "motivation_score": self.motivation_score,
            "form_score": self.form_score,
            "composite": self.composite,
            "reasons": self.reasons,
        }


@dataclass
class Coupon:
    legs: list[Pick]
    combined_prob: float = field(init=False)
    combined_odds: float = field(init=False)

    def __post_init__(self) -> None:
        p = 1.0
        o = 1.0
        for leg in self.legs:
            p *= leg.prob
            if leg.book_odds is not None:
                o *= leg.book_odds
            else:
                # If we have no odds, imply 1/prob as a fallback estimate.
                o *= (1.0 / leg.prob) if leg.prob > 0 else 1.0
        self.combined_prob = p
        self.combined_odds = o

    def to_dict(self) -> dict[str, Any]:
        return {
            "legs": [leg.to_dict() for leg in self.legs],
            "combined_prob": self.combined_prob,
            "combined_odds": self.combined_odds,
            "num_legs": len(self.legs),
        }


# ---- signal scoring helpers ----------------------------------------------

def _base_market(market: str) -> str:
    """Normalize 'over_under_2.5' → 'over_under' for diversity enforcement."""
    if market.startswith("over_under"):
        return "over_under"
    if market.startswith("asian_handicap"):
        return "asian_handicap"
    if "_" in market:
        return market.split("_", 1)[0] if market != "1X2" else market
    return market


def _value_edge(model_prob: float, book_prob: float | None) -> float:
    if book_prob is None or book_prob <= 0:
        return 0.0
    # Positive edge = we think the event is more likely than the book does.
    return (model_prob / book_prob) - 1.0


def _motivation_score(
    market: str, selection: str, context: dict[str, Any] | None
) -> tuple[float, str | None]:
    """Return (score in [-1,1], reason-string or None).

    Positive means the motivation profile supports this selection direction;
    negative means it actively argues against. We only speak up when the signal
    is strong enough to warrant a user-visible reason.
    """
    if not context:
        return 0.0, None
    home_mot = context.get("home_motivation") or {}
    away_mot = context.get("away_motivation") or {}

    # For 1X2 and double_chance, motivation of the side we're picking matters.
    if market in ("1X2", "double_chance"):
        if selection in ("1", "1X"):
            intensity = max(
                home_mot.get("relegation_risk", 0),
                home_mot.get("title_push", 0),
                home_mot.get("europe_push", 0),
            )
            dead = away_mot.get("dead_rubber", 0)
            score = min(1.0, intensity + 0.5 * dead)
            if intensity >= 0.6:
                reason = _top_motivation_reason(home_mot, side="ev sahibi")
                return score, reason
            if dead >= 0.5:
                return score, f"Deplasman takımı dead rubber ({dead:.0%})"
            return score, None
        if selection in ("2", "X2"):
            intensity = max(
                away_mot.get("relegation_risk", 0),
                away_mot.get("title_push", 0),
                away_mot.get("europe_push", 0),
            )
            dead = home_mot.get("dead_rubber", 0)
            score = min(1.0, intensity + 0.5 * dead)
            if intensity >= 0.6:
                reason = _top_motivation_reason(away_mot, side="deplasman")
                return score, reason
            if dead >= 0.5:
                return score, f"Ev sahibi dead rubber ({dead:.0%})"
            return score, None

    # For goal-based markets, BOTH sides being motivated raises goal expectation;
    # both being dead lowers it. So "over" wants high combined intensity, "under"
    # the opposite.
    if market.startswith("over_under") or market == "btts":
        combined_intensity = (
            home_mot.get("intensity", 0) + away_mot.get("intensity", 0)
        ) / 2
        combined_dead = (
            home_mot.get("dead_rubber", 0) + away_mot.get("dead_rubber", 0)
        ) / 2
        if selection in ("over", "yes"):
            if combined_intensity >= 0.5:
                return combined_intensity, (
                    f"İki takımın da oynayacağı bir şey var (intensity {combined_intensity:.0%})"
                )
            if combined_dead >= 0.4:
                return -combined_dead, None  # argues against over
            return 0.0, None
        if selection in ("under", "no"):
            if combined_dead >= 0.4:
                return combined_dead, (
                    f"İki takım da dead rubber — gol beklentisi düşük"
                )
            return 0.0, None

    return 0.0, None


def _top_motivation_reason(mot: dict[str, Any], side: str) -> str | None:
    """Pick the single strongest stake and express it in Turkish."""
    team = mot.get("team") or ""
    stakes = [
        ("relegation_risk", "küme düşme hattına yakın"),
        ("title_push", "şampiyonluk yarışında"),
        ("europe_push", "Avrupa kupası hattında"),
    ]
    best = max(stakes, key=lambda s: mot.get(s[0], 0))
    val = mot.get(best[0], 0)
    if val < 0.5:
        return None
    return f"{team} ({side}) {best[1]} ({val:.0%})"


def _form_score(
    market: str, selection: str, context: dict[str, Any] | None
) -> tuple[float, str | None]:
    """Placeholder until context carries recent-form metrics.

    The standings-derived intensity already proxies season-long stakes; a short
    rolling-form signal would complement it but needs its own feature. Leaving
    a 0 contribution keeps the composite honest — we don't fabricate a score
    we can't back up.
    """
    return 0.0, None


# ---- pick enumeration -----------------------------------------------------

def _enumerate_picks(
    *,
    match_id: int,
    home: str,
    away: str,
    kickoff: str,
    league: str,
    payload: dict[str, Any],
    odds_by_key: dict[tuple[str, str], float],
    allowed_markets: set[str] | None,
    min_prob: float,
) -> list[Pick]:
    """Generate every candidate Pick for one match, across all markets."""
    context = payload.get("context")
    out: list[Pick] = []

    def emit(market: str, selection: str, selection_label: str, prob: float) -> None:
        base = _base_market(market)
        if allowed_markets is not None and base not in allowed_markets:
            return
        if prob < min_prob:
            return

        book_odds = odds_by_key.get((market, selection))
        if book_odds is None:
            # Look up by base market too — odds table uses e.g. "OU_2.5" keys.
            # We try both naming conventions so we don't silently lose the edge signal.
            book_odds = odds_by_key.get((market.replace("over_under_", "OU_"), selection))
        book_prob = (1.0 / book_odds) if book_odds else None
        edge = _value_edge(prob, book_prob)

        mot_score, mot_reason = _motivation_score(market, selection, context)
        form_score, form_reason = _form_score(market, selection, context)

        # Composite in [0, 1]. Edge is unbounded in principle so clamp its
        # contribution to keep a single big edge from swamping everything else.
        clamped_edge = max(-0.3, min(0.3, edge)) / 0.3  # now in [-1, 1]
        composite = (
            _W_PROB * prob
            + _W_VALUE * (0.5 + 0.5 * clamped_edge)
            + _W_MOTIVATION * (0.5 + 0.5 * mot_score)
            + _W_FORM * (0.5 + 0.5 * form_score)
        )

        reasons: list[str] = []
        reasons.append(f"Model olasılığı %{prob*100:.0f}")
        if book_odds is not None:
            if edge > 0.03:
                reasons.append(
                    f"Piyasa oranı {book_odds:.2f} (impl %{book_prob*100:.0f}) — "
                    f"model %{edge*100:.0f} edge görüyor"
                )
            elif edge < -0.05:
                reasons.append(
                    f"Piyasa bu seçimde daha iddialı (oran {book_odds:.2f})"
                )
            else:
                reasons.append(f"Piyasa oranı {book_odds:.2f} — model ile uyumlu")
        if mot_reason:
            reasons.append(mot_reason)
        if form_reason:
            reasons.append(form_reason)

        out.append(
            Pick(
                match_id=match_id,
                home_team=home,
                away_team=away,
                kickoff=kickoff,
                league=league,
                market=market,
                market_label=MARKET_LABELS.get(base, base),
                selection=selection,
                selection_label=selection_label,
                prob=prob,
                book_odds=book_odds,
                book_prob=book_prob,
                value_edge=edge,
                motivation_score=mot_score,
                form_score=form_score,
                composite=composite,
                reasons=reasons,
            )
        )

    if "1X2" in payload:
        for sel, p in payload["1X2"].items():
            emit("1X2", sel, SELECTION_LABELS.get(sel, sel), float(p))
    if "double_chance" in payload:
        for sel, p in payload["double_chance"].items():
            emit("double_chance", sel, SELECTION_LABELS.get(sel, sel), float(p))
    if "over_under" in payload:
        for line, ou in payload["over_under"].items():
            for sel, p in ou.items():
                emit(
                    f"over_under_{line}",
                    sel,
                    f"{line} {SELECTION_LABELS.get(sel, sel)}",
                    float(p),
                )
    if "btts" in payload:
        for sel, p in payload["btts"].items():
            emit("btts", sel, SELECTION_LABELS.get(sel, sel), float(p))
    if "odd_even" in payload:
        for sel, p in payload["odd_even"].items():
            emit("odd_even", sel, SELECTION_LABELS.get(sel, sel), float(p))

    return out


# ---- coupon composition --------------------------------------------------

def _pick_best_per_match(picks: list[Pick]) -> list[Pick]:
    """Collapse to one best-composite pick per match."""
    by_match: dict[int, Pick] = {}
    for p in picks:
        cur = by_match.get(p.match_id)
        if cur is None or p.composite > cur.composite:
            by_match[p.match_id] = p
    return list(by_match.values())


def _compose_coupon(
    picks: list[Pick],
    *,
    num_legs: int,
    min_combined_odds: float,
    enforce_market_diversity: bool,
    excluded_match_ids: set[int] | None = None,
) -> Coupon | None:
    """Greedy: maximize composite subject to diversity + odds floor.

    We iterate candidates by composite desc, add if (a) the match isn't already
    used, (b) the base market hasn't been used when diversity is on, and stop
    once we've hit num_legs. If the resulting combined_odds is below the floor,
    we retry allowing the next candidates to substitute lower-composite but
    higher-odds picks into the weakest slot.
    """
    excluded = excluded_match_ids or set()
    sorted_picks = sorted(
        [p for p in picks if p.match_id not in excluded],
        key=lambda p: p.composite,
        reverse=True,
    )

    legs: list[Pick] = []
    used_matches: set[int] = set()
    used_markets: set[str] = set()
    for p in sorted_picks:
        if p.match_id in used_matches:
            continue
        base = _base_market(p.market)
        if enforce_market_diversity and base in used_markets:
            continue
        legs.append(p)
        used_matches.add(p.match_id)
        used_markets.add(base)
        if len(legs) == num_legs:
            break

    if len(legs) < num_legs:
        return None

    coupon = Coupon(legs=legs)
    if coupon.combined_odds >= min_combined_odds:
        return coupon

    # Below the odds floor: try swapping the weakest leg (by value_edge) for a
    # higher-odds alternative from a market we haven't yet used.
    for _ in range(num_legs):
        weakest_idx = min(
            range(len(legs)),
            key=lambda i: legs[i].book_odds or (1 / max(legs[i].prob, 1e-6)),
        )
        weakest = legs[weakest_idx]
        replacement = None
        for candidate in sorted_picks:
            if candidate.match_id in used_matches:
                continue
            cand_base = _base_market(candidate.market)
            if enforce_market_diversity and cand_base in (used_markets - {_base_market(weakest.market)}):
                continue
            cand_odds = candidate.book_odds or (1 / max(candidate.prob, 1e-6))
            weak_odds = weakest.book_odds or (1 / max(weakest.prob, 1e-6))
            if cand_odds > weak_odds:
                replacement = candidate
                break
        if replacement is None:
            break
        used_matches.remove(weakest.match_id)
        used_markets.discard(_base_market(weakest.market))
        legs[weakest_idx] = replacement
        used_matches.add(replacement.match_id)
        used_markets.add(_base_market(replacement.market))
        coupon = Coupon(legs=legs)
        if coupon.combined_odds >= min_combined_odds:
            return coupon

    # Still under target — return it anyway; caller can decide whether to publish.
    return coupon


def suggest_coupons(
    match_predictions: list[dict[str, Any]],
    *,
    min_prob_per_leg: float = 0.55,
    num_legs: int = 3,
    allowed_markets: set[str] | None = None,
    max_coupons: int = 5,
    min_combined_odds: float = 1.6,
    enforce_market_diversity: bool = True,
    odds_by_match: dict[int, dict[tuple[str, str], float]] | None = None,
) -> dict[str, Any]:
    """Generate composite-scored coupon suggestions.

    Parameters
    ----------
    match_predictions : list of {match_id, home_team, away_team, kickoff, league, payload}
    min_prob_per_leg : floor below which a pick is excluded outright
    num_legs : legs per coupon
    allowed_markets : base-market whitelist (None = all)
    max_coupons : total coupons to return (primary + alternatives)
    min_combined_odds : composer rejects/swaps to hit this (default 1.6 → meaningful payout)
    enforce_market_diversity : no two legs from the same base market
    odds_by_match : {match_id: {(market, selection): decimal_odds}}
    """
    odds_by_match = odds_by_match or {}

    all_picks: list[Pick] = []
    for m in match_predictions:
        picks = _enumerate_picks(
            match_id=m["match_id"],
            home=m["home_team"],
            away=m["away_team"],
            kickoff=m["kickoff"],
            league=m["league"],
            payload=m["payload"],
            odds_by_key=odds_by_match.get(m["match_id"], {}),
            allowed_markets=allowed_markets,
            min_prob=min_prob_per_leg,
        )
        all_picks.extend(picks)

    # Composer sees ALL picks across all markets; it decides per-match which
    # selection to use. Collapsing to one-per-match first would kill diversity
    # because every match's highest-composite pick is always "0.5 Üst".
    all_picks.sort(key=lambda p: p.composite, reverse=True)

    coupons: list[Coupon] = []
    excluded: set[int] = set()
    for _ in range(max_coupons):
        c = _compose_coupon(
            all_picks,
            num_legs=num_legs,
            min_combined_odds=min_combined_odds,
            enforce_market_diversity=enforce_market_diversity,
            excluded_match_ids=excluded,
        )
        if c is None:
            break
        coupons.append(c)
        for leg in c.legs:
            excluded.add(leg.match_id)

    primary = coupons[0] if coupons else None
    alternatives = coupons[1:]

    # Bankos (single-leg "sure" picks): use the best-composite pick per match,
    # ranked; this is a different view from the coupon composer.
    best_per_match = _pick_best_per_match(all_picks)
    best_per_match.sort(key=lambda p: p.composite, reverse=True)
    bankos = [Coupon(legs=[p]) for p in best_per_match[:5]]

    return {
        "primary": primary.to_dict() if primary else None,
        "alternatives": [c.to_dict() for c in alternatives],
        "bankos": [c.to_dict() for c in bankos],
        "all_picks": [p.to_dict() for p in best_per_match],
        "filters": {
            "min_prob_per_leg": min_prob_per_leg,
            "num_legs": num_legs,
            "allowed_markets": sorted(allowed_markets) if allowed_markets else None,
            "min_combined_odds": min_combined_odds,
            "enforce_market_diversity": enforce_market_diversity,
            "weights": {
                "prob": _W_PROB,
                "value": _W_VALUE,
                "motivation": _W_MOTIVATION,
                "form": _W_FORM,
            },
        },
    }
