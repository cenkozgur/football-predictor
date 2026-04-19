"""Rolling-form features per team.

The Dixon-Coles strengths are fit over the whole season with a time decay,
which captures *ability* but is slow to react to streaks. A team that's
lost its starting striker three weeks ago and has scored 0.5 goals per
match since won't show up weaker in DC until the decayed average catches
up. Form fills that gap:

    - goals_for / goals_against over the last N matches (home+away blended)
    - win rate, clean-sheet rate, fail-to-score rate
    - "scored both halves" frequency (proxy for attacking consistency)
    - home/away split kept separately because home advantage is large

Form is derived purely from matches already in the DB — no new ingester.
The output is a small scalar summary that `adjust.py` can multiply into
the λ/μ alongside motivation, and a larger per-team dict that the pick
engine uses to craft Turkish reasons ("son 5 deplasmanda 2.4 gol
yiyorlar" → supports an Over pick).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.match import Match


# How many recent matches define "form". Too short = noisy (one bad match
# dominates); too long = same as full-season fit. 6 is the usual football
# analyst convention for an informative but current window.
_FORM_WINDOW = 6


@dataclass
class TeamForm:
    team_id: int
    team_name: str

    games: int
    wins: int
    draws: int
    losses: int
    goals_for: int
    goals_against: int

    # Rolling rates in [0, 1]
    win_rate: float = 0.0
    clean_sheet_rate: float = 0.0
    fail_to_score_rate: float = 0.0

    # Per-game averages
    gf_per_game: float = 0.0
    ga_per_game: float = 0.0

    # Home/away split so we don't dilute venue effects
    home_gf_per_game: float = 0.0
    home_ga_per_game: float = 0.0
    away_gf_per_game: float = 0.0
    away_ga_per_game: float = 0.0

    # Composite: sum of a team's relative strength change vs its own season avg.
    # > 0 means in form, < 0 means slump. Capped to roughly ±1 for downstream math.
    form_delta: float = 0.0

    reasons: list[str] = field(default_factory=list)


def compute_team_form(
    db: Session,
    team_id: int,
    team_name: str,
    league: str,
    season: str,
    asof: datetime,
    *,
    window: int = _FORM_WINDOW,
) -> TeamForm | None:
    """Build a TeamForm from the team's last `window` finished matches
    before `asof` in the given league+season.
    """
    asof_naive = asof.replace(tzinfo=None) if asof.tzinfo else asof
    stmt = (
        select(Match)
        .where(Match.league == league)
        .where(Match.season == season)
        .where(Match.status == "finished")
        .where(Match.ft_home.is_not(None), Match.ft_away.is_not(None))
        .where(Match.kickoff < asof_naive)
        .where(or_(Match.home_team_id == team_id, Match.away_team_id == team_id))
        .order_by(Match.kickoff.desc())
        .limit(window)
    )
    rows = db.scalars(stmt).all()
    if not rows:
        return None

    f = TeamForm(
        team_id=team_id, team_name=team_name,
        games=len(rows), wins=0, draws=0, losses=0,
        goals_for=0, goals_against=0,
    )

    home_games = 0
    home_gf = 0
    home_ga = 0
    away_games = 0
    away_gf = 0
    away_ga = 0
    clean_sheets = 0
    fails_to_score = 0
    # Season averages for this team so we can compute form_delta (current vs
    # trend). The "trend" baseline is every earlier-than-form-window finished
    # match this team played in this season.
    season_stmt = (
        select(Match)
        .where(Match.league == league, Match.season == season)
        .where(Match.status == "finished")
        .where(Match.kickoff < asof_naive)
        .where(or_(Match.home_team_id == team_id, Match.away_team_id == team_id))
    )
    season_rows = db.scalars(season_stmt).all()

    for m in rows:
        is_home = m.home_team_id == team_id
        gf = m.ft_home if is_home else m.ft_away
        ga = m.ft_away if is_home else m.ft_home
        f.goals_for += gf
        f.goals_against += ga
        if gf > ga:
            f.wins += 1
        elif gf < ga:
            f.losses += 1
        else:
            f.draws += 1
        if ga == 0:
            clean_sheets += 1
        if gf == 0:
            fails_to_score += 1
        if is_home:
            home_games += 1
            home_gf += gf
            home_ga += ga
        else:
            away_games += 1
            away_gf += gf
            away_ga += ga

    f.win_rate = f.wins / f.games
    f.clean_sheet_rate = clean_sheets / f.games
    f.fail_to_score_rate = fails_to_score / f.games
    f.gf_per_game = f.goals_for / f.games
    f.ga_per_game = f.goals_against / f.games
    if home_games:
        f.home_gf_per_game = home_gf / home_games
        f.home_ga_per_game = home_ga / home_games
    if away_games:
        f.away_gf_per_game = away_gf / away_games
        f.away_ga_per_game = away_ga / away_games

    # Form delta: how much better/worse is the team scoring and conceding
    # recently vs. their whole-season baseline. A team that averages 1.5 GF
    # all season but 2.4 GF in the last 6 is in form (+0.9 attack).
    if len(season_rows) >= f.games + 3:  # need a baseline to compare to
        base_gf = 0
        base_ga = 0
        for m in season_rows:
            is_home = m.home_team_id == team_id
            base_gf += m.ft_home if is_home else m.ft_away
            base_ga += m.ft_away if is_home else m.ft_home
        base_gf_pg = base_gf / len(season_rows)
        base_ga_pg = base_ga / len(season_rows)
        # Normalize by baseline so "1 extra goal from a 1.0 team" and "1 extra
        # goal from a 3.0 team" aren't treated the same magnitude.
        att_delta = (f.gf_per_game - base_gf_pg) / max(base_gf_pg, 0.5)
        def_delta = (base_ga_pg - f.ga_per_game) / max(base_ga_pg, 0.5)
        f.form_delta = max(-1.0, min(1.0, 0.5 * (att_delta + def_delta)))

    # Turkish user-facing reasons — only populate when the signal is strong.
    if f.games >= 3:
        if f.form_delta >= 0.25:
            f.reasons.append(
                f"Son {f.games} maçta formda "
                f"({f.gf_per_game:.1f} attığı – {f.ga_per_game:.1f} yediği gol)"
            )
        elif f.form_delta <= -0.25:
            f.reasons.append(
                f"Son {f.games} maçta düşüşte "
                f"({f.gf_per_game:.1f} attığı – {f.ga_per_game:.1f} yediği gol)"
            )
        if f.clean_sheet_rate >= 0.5 and f.games >= 4:
            f.reasons.append(
                f"Son {f.games} maçın %{int(f.clean_sheet_rate*100)}'ında gol yemedi"
            )
        if f.fail_to_score_rate >= 0.5 and f.games >= 4:
            f.reasons.append(
                f"Son {f.games} maçın %{int(f.fail_to_score_rate*100)}'ında gol atamadı"
            )

    return f
