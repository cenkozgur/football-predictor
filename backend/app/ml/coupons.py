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


# Composite-score weights — value-first, not probability-first.
#
# Earlier we weighted raw model probability at 40%, which meant any 95%-sure
# pick with zero edge against the market (Bilyoner agreeing with us) still
# scored high and ended up in coupons. The user's point: if we're merely
# betting on Bilyoner's own favorite, we have no reason to expect to beat
# Bilyoner. Edge + motivation + form are what distinguish our picks from
# the market's picks. Probability is a floor (via min_prob), not a score driver.
#
# Availability is smaller than motivation because it's a secondary signal:
# the market already prices in announced lineups. We use it mostly for the
# direction (a team losing its striker shouldn't be a 1X pick) and for a
# user-visible reason, not as a primary discriminator.
_W_PROB = 0.15
_W_VALUE = 0.45
_W_MOTIVATION = 0.20
_W_FORM = 0.10
_W_AVAILABILITY = 0.10

# Hard edge floor: a pick is only coupon-eligible if our model's probability
# beats the bookmaker's implied probability by at least this margin. Below
# this we're not offering anything Bilyoner doesn't already price in.
# Picks with no odds data at all are also rejected — we refuse to play blind.
_MIN_VALUE_EDGE = 0.03


# Per-league EV+ policy — derived from the backtest_ev_search grid scan on
# 2026-04-23. Each entry narrows the normal pass to (markets, edge, min_prob)
# that historically produced positive ROI in walk-forward. Leagues absent
# from this dict are blocked from generating primary coupons; they fall
# through to the fallback pass (which still honors min_prob but no edge
# requirement, and is clearly tagged in the UI).
#
# Entries verified against both 6-mo and 18-mo windows. Edge/prob chosen as
# the conservative side of the two (smaller sample → higher noise, so we
# lean toward the 18-mo number with more picks).
#
# SP1 (La Liga) and F1 (Ligue 1) deliberately omitted — both windows were
# net-negative. When they move into positive we'll add them.
#
#     dict value = (allowed_base_markets, min_edge, min_prob)
_LEAGUE_POLICY: dict[str, tuple[set[str], float, float]] = {
    "E0":  ({"over_under"},   0.03, 0.55),  # EPL — OU, +3.8% 18mo
    "D1":  ({"over_under"},   0.03, 0.60),  # Bundesliga — OU, +3.1% 18mo
    "I1":  ({"1X2"},          0.01, 0.50),  # Serie A — 1X2, +4.6% 18mo
}


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
    availability_score: float = 0.0  # -1..1; positive = roster advantage
    composite: float = 0.0   # weighted blend in [0, 1]
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
            "availability_score": self.availability_score,
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


def _value_edge(
    model_prob: float,
    book_prob: float | None,
    overround_multiplier: float = 1.0,
) -> float:
    """Edge vs. the fair (de-vigged) bookmaker probability.

    Raw implied probability from a single decimal odds includes the book's
    margin; comparing our model to that is unfair — we'd need to beat both
    the market AND the vig. When `overround_multiplier` > 1 is passed, it
    is used to deflate book_prob back to the book's "no-vig" estimate.
    """
    if book_prob is None or book_prob <= 0:
        return 0.0
    fair_prob = book_prob / overround_multiplier
    return (model_prob / fair_prob) - 1.0


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
    """Rolling-form support for this pick, in [-1, 1].

    We read the per-team form dicts attached by predict_upcoming.py (the
    standings-derived motivation already proxies season stakes; form_delta
    captures the current streak that DC's time-decayed strengths are too slow
    to react to). The returned score nudges the composite up or down, and the
    accompanying Turkish reason — only populated when the signal clearly backs
    the selection — feeds the UI's "neden" panel.
    """
    if not context:
        return 0.0, None
    hf = context.get("home_form") or {}
    af = context.get("away_form") or {}
    hd = float(hf.get("form_delta", 0.0)) if hf else 0.0
    ad = float(af.get("form_delta", 0.0)) if af else 0.0

    base = _base_market(market)
    score = 0.0
    reason: str | None = None

    if base == "1X2":
        if selection == "1":
            score = max(-1.0, min(1.0, hd - ad))
            if hd >= 0.25 and ad <= 0.0 and hf.get("reasons"):
                reason = f"Ev: {hf['reasons'][0]}"
        elif selection == "2":
            score = max(-1.0, min(1.0, ad - hd))
            if ad >= 0.25 and hd <= 0.0 and af.get("reasons"):
                reason = f"Dep: {af['reasons'][0]}"
        else:  # X — draw supported when both are flat
            score = -max(abs(hd), abs(ad))
    elif base == "double_chance":
        if selection == "1X":
            score = max(-1.0, min(1.0, hd - 0.5 * ad))
        elif selection == "X2":
            score = max(-1.0, min(1.0, ad - 0.5 * hd))
        elif selection == "12":
            score = max(-1.0, min(1.0, 0.5 * (abs(hd) + abs(ad))))
    elif base == "btts":
        # Both sides scoring more than usual → yes; either failing to score → no.
        att = 0.5 * (hd + ad)
        if selection == "yes":
            score = max(-1.0, min(1.0, att))
        else:
            score = max(-1.0, min(1.0, -att))
    elif base == "over_under":
        # Over: both sides' attack delta helps. Under: both defenses tighter.
        att = 0.5 * (hd + ad)
        if selection == "over":
            score = max(-1.0, min(1.0, att))
        else:
            score = max(-1.0, min(1.0, -att))
    elif base == "odd_even":
        score = 0.0  # form tells us nothing about parity

    return score, reason


# Positions we treat as "decisive": losing one of these is a stronger signal
# than losing a 12th-15th midfield rotator. api-football position strings are
# free-form so we match by substring rather than exact equality.
_KEY_POSITION_PREFIXES = ("goalkeeper", "forward", "attacker")


def _availability_score(
    market: str, selection: str, context: dict[str, Any] | None
) -> tuple[float, str | None]:
    """Roster-availability signal, in [-1, 1].

    Reads `home_availability` / `away_availability` attached by
    predict_upcoming. Positive score = the picked side has a lineup advantage
    (opponent missing key players or has more absences). Produces a Turkish
    reason when a forward or goalkeeper is out, since those are the ones the
    model most under-weights.
    """
    if not context:
        return 0.0, None
    home = context.get("home_availability") or {}
    away = context.get("away_availability") or {}

    home_absent = int(home.get("key_absent_count", 0) or 0)
    away_absent = int(away.get("key_absent_count", 0) or 0)

    def _has_key_position(row: dict[str, Any]) -> str | None:
        for a in row.get("key_absences") or []:
            pos = (a.get("position") or "").lower()
            if any(pos.startswith(p) for p in _KEY_POSITION_PREFIXES):
                return a.get("name") or a.get("position")
        return None

    home_key = _has_key_position(home)
    away_key = _has_key_position(away)

    # Normalize absent counts into [-1, 1] differential. 3 absences is already
    # a notable disruption; we clip there rather than scale linearly to ∞.
    diff = max(-3, min(3, away_absent - home_absent)) / 3.0

    base = _base_market(market)
    score = 0.0
    reason: str | None = None

    if base == "1X2":
        if selection == "1":
            score = diff  # away missing > home missing → favors home
            if away_key and away_absent >= 2:
                reason = f"Deplasmanda {away_key} yok"
        elif selection == "2":
            score = -diff
            if home_key and home_absent >= 2:
                reason = f"Ev sahibinde {home_key} yok"
        else:  # X — draws are modestly supported when both sides are shorthanded
            score = -abs(diff) * 0.5
    elif base == "double_chance":
        if selection == "1X":
            score = max(-1.0, min(1.0, diff * 0.7))
        elif selection == "X2":
            score = max(-1.0, min(1.0, -diff * 0.7))
        elif selection == "12":
            # Either side winning is easier when the other is weakened, so the
            # *magnitude* of asymmetry helps; we don't care about direction.
            score = min(1.0, abs(diff) * 0.5)
    elif base == "btts":
        # Lose a GK → more goals expected. Lose a striker → fewer.
        # Without position detail we'd be guessing; use key_absent_count as a
        # crude signal: heavy absences on either side depress btts_yes.
        heavy = max(home_absent, away_absent) >= 3
        if selection == "yes":
            score = -0.5 if heavy else 0.0
        else:
            score = 0.5 if heavy else 0.0
    elif base == "over_under":
        heavy = max(home_absent, away_absent) >= 3
        if selection == "over":
            score = -0.5 if heavy else 0.0
        else:
            score = 0.5 if heavy else 0.0

    return score, reason


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
    require_edge: bool = True,
) -> list[Pick]:
    """Generate every candidate Pick for one match, across all markets."""
    context = payload.get("context")
    out: list[Pick] = []

    # Apply per-league EV+ policy on top of the generic allowed_markets/
    # min_prob/edge gates. Only applies to the normal (require_edge) pass;
    # the fallback pass bypasses league policy so that on boş-slate days
    # we can still snapshot a tracked pick from any league.
    league_markets: set[str] | None = None
    league_min_edge: float = _MIN_VALUE_EDGE
    league_min_prob_floor: float = 0.0
    if require_edge:
        pol = _LEAGUE_POLICY.get(league)
        if pol is None:
            # League has no verified edge strategy — suppress every pick in the
            # normal pass. Fallback pass will still run if the caller asks.
            return out
        league_markets, league_min_edge, league_min_prob_floor = pol

    # Pre-compute per-market overround (book's total implied prob across all
    # selections) so we can de-vig before scoring edge. Key: "1X2",
    # "over_under_2.5", "btts", etc. A market only gets a real de-vig factor
    # if odds for every selection are present; otherwise we fall back to 1.0
    # (raw implied prob), which is conservative — we won't over-claim edge.
    overrounds: dict[str, float] = {}

    def _market_keys(market_prefix: str, selections: list[str]) -> list[tuple[str, str]]:
        alt_prefix = market_prefix
        if market_prefix.startswith("over_under_"):
            alt_prefix = f"OU_{market_prefix[len('over_under_'):]}"
        out_keys: list[tuple[str, str]] = []
        for sel in selections:
            out_keys.append((market_prefix, sel))
            if alt_prefix != market_prefix:
                out_keys.append((alt_prefix, sel))
        return out_keys

    def _overround(market_prefix: str, selections: list[str]) -> float:
        total = 0.0
        for sel in selections:
            o = odds_by_key.get((market_prefix, sel))
            if o is None and market_prefix.startswith("over_under_"):
                o = odds_by_key.get((f"OU_{market_prefix[len('over_under_'):]}", sel))
            if o is None or o <= 0:
                return 1.0
            total += 1.0 / o
        return total if total > 0 else 1.0

    overrounds["1X2"] = _overround("1X2", ["1", "X", "2"])
    overrounds["btts"] = _overround("btts", ["yes", "no"])
    if "over_under" in payload:
        for line in payload["over_under"].keys():
            overrounds[f"over_under_{line}"] = _overround(
                f"over_under_{line}", ["over", "under"]
            )

    def emit(market: str, selection: str, selection_label: str, prob: float) -> None:
        base = _base_market(market)
        if allowed_markets is not None and base not in allowed_markets:
            return
        # Per-league market whitelist (only applies in require_edge pass).
        if league_markets is not None and base not in league_markets:
            return
        if prob < min_prob or prob < league_min_prob_floor:
            return

        book_odds = odds_by_key.get((market, selection))
        if book_odds is None:
            # Look up by base market too — odds table uses e.g. "OU_2.5" keys.
            # We try both naming conventions so we don't silently lose the edge signal.
            book_odds = odds_by_key.get((market.replace("over_under_", "OU_"), selection))
        book_prob = (1.0 / book_odds) if book_odds else None
        overround = overrounds.get(market, 1.0)
        edge = _value_edge(prob, book_prob, overround_multiplier=overround)

        # Strategy gate: require a positive edge against the market. In the
        # normal pass we won't play picks we can't compare — if odds are
        # missing we skip. In the fallback pass (`require_edge=False`), we
        # also accept odds-less picks because smaller leagues' Big-5 CSVs
        # are sparse and otherwise we'd snapshot nothing on quiet days. When
        # that happens we synthesize `book_odds = 1 / prob` (i.e. zero-edge
        # implied odds) so the coupon still has a payout figure to display;
        # edge stays 0 and a reason string notes the provenance.
        synthesized_odds = False
        if book_odds is None:
            if require_edge:
                return
            if prob <= 0:
                return
            book_odds = 1.0 / prob
            book_prob = prob
            edge = 0.0
            synthesized_odds = True
        if require_edge and edge < max(_MIN_VALUE_EDGE, league_min_edge):
            return

        mot_score, mot_reason = _motivation_score(market, selection, context)
        form_score, form_reason = _form_score(market, selection, context)
        avail_score, avail_reason = _availability_score(market, selection, context)

        # Composite in [0, 1]. Edge is unbounded in principle so clamp its
        # contribution to keep a single big edge from swamping everything else.
        clamped_edge = max(-0.3, min(0.3, edge)) / 0.3  # now in [-1, 1]
        composite = (
            _W_PROB * prob
            + _W_VALUE * (0.5 + 0.5 * clamped_edge)
            + _W_MOTIVATION * (0.5 + 0.5 * mot_score)
            + _W_FORM * (0.5 + 0.5 * form_score)
            + _W_AVAILABILITY * (0.5 + 0.5 * avail_score)
        )

        reasons: list[str] = []
        reasons.append(f"Model olasılığı %{prob*100:.0f}")
        if synthesized_odds:
            reasons.append(
                f"Piyasa oranı bulunamadı — model olasılığından tahmini oran "
                f"{book_odds:.2f} kullanıldı"
            )
        elif book_odds is not None:
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
        if avail_reason:
            reasons.append(avail_reason)

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
                availability_score=avail_score,
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
    min_legs: int = 1,
    max_legs: int = 4,
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

    # Try legs from max down to min — we'd rather offer one high-conviction
    # single pick than refuse to suggest anything when diversity/odds don't
    # support a 3-legger. Each size is attempted until we've produced
    # `max_coupons` total or the pick pool is exhausted.
    coupons: list[Coupon] = []
    excluded: set[int] = set()
    leg_sizes = list(range(min(num_legs, max_legs), max(min_legs, 1) - 1, -1))
    for legs_target in leg_sizes:
        while len(coupons) < max_coupons:
            c = _compose_coupon(
                all_picks,
                num_legs=legs_target,
                min_combined_odds=min_combined_odds,
                enforce_market_diversity=enforce_market_diversity,
                excluded_match_ids=excluded,
            )
            if c is None:
                break
            if c.combined_odds < min_combined_odds:
                break
            coupons.append(c)
            for leg in c.legs:
                excluded.add(leg.match_id)
        if len(coupons) >= max_coupons:
            break

    primary = coupons[0] if coupons else None
    alternatives = coupons[1:]
    is_fallback = False

    # Fallback: edge gate hiçbir pick'i geçiremediyse (tipik ince slate
    # günlerinde — Pazartesi/Salı) track record birikmesi için en yüksek
    # composite'li tek bacağı sun. Açıkça etiketli (is_fallback=True) ki UI
    # "bugün güçlü bir value görmedik" mesajını gösterebilsin.
    if primary is None:
        fallback_picks: list[Pick] = []
        for m in match_predictions:
            fallback_picks.extend(
                _enumerate_picks(
                    match_id=m["match_id"],
                    home=m["home_team"],
                    away=m["away_team"],
                    kickoff=m["kickoff"],
                    league=m["league"],
                    payload=m["payload"],
                    odds_by_key=odds_by_match.get(m["match_id"], {}),
                    allowed_markets=allowed_markets,
                    min_prob=min_prob_per_leg,
                    require_edge=False,
                )
            )
        if fallback_picks:
            fallback_picks.sort(key=lambda p: p.composite, reverse=True)
            top = fallback_picks[0]
            # Prefer picks with real odds when they exist — synthesized-odds
            # picks are still useful but a real market quote is more trustworthy.
            real_odds_picks = [
                p for p in fallback_picks
                if "tahmini oran" not in " ".join(p.reasons)
            ]
            if real_odds_picks:
                top = real_odds_picks[0]
            top.reasons.insert(
                0,
                "Bugün yüksek value bulamadık — model'in en güvendiği tekli seçim",
            )
            primary = Coupon(legs=[top])
            is_fallback = True

    # Bankos (single-leg "sure" picks): use the best-composite pick per match,
    # ranked; this is a different view from the coupon composer.
    best_per_match = _pick_best_per_match(all_picks)
    best_per_match.sort(key=lambda p: p.composite, reverse=True)
    bankos = [Coupon(legs=[p]) for p in best_per_match[:5]]

    primary_dict = primary.to_dict() if primary else None
    if primary_dict is not None:
        primary_dict["is_fallback"] = is_fallback

    return {
        "primary": primary_dict,
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
                "availability": _W_AVAILABILITY,
            },
        },
    }
