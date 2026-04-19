"""Per-team motivation signals derived from a standings snapshot.

High-level idea: the Dixon-Coles baseline captures *ability*, but not *intent*.
A mid-table team in mid-October and the same team on matchday 37 with nothing
to play for are modeled as identical — in reality their effort is not.

Each signal is a 0..1 float where 1 means "this stake is very alive right now".
We also derive a scalar `intensity` in [0, 1] that summarizes overall
motivation and a short list of `reasons` (Turkish strings) we can surface
in the UI as the "neden" panel for high-x picks.

The signals deliberately stay coarse and explainable — we prefer a feature
we can show to the user over a black-box embedding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.features.standings import Standings, TeamRow


# League-level thresholds for stake-relevant positions. Keeping this as a dict
# so that 18/20-team top divisions and 16-team odd leagues are handled
# correctly without per-league special-casing at the call site.
#
# `relegation_from_bottom`: ranks counting from the bottom that are in danger
# `europe_top`: ranks (from the top) that qualify for some European spot
# `title_top`: ranks that are realistically in the title race
_DEFAULTS = {"relegation_from_bottom": 3, "europe_top": 6, "title_top": 2}

_LEAGUE_POSITION_HINTS: dict[str, dict[str, int]] = {
    # 20-team Big-5: 3 relegation, top-6 Europe push, top-2 title race
    "E0": _DEFAULTS, "SP1": _DEFAULTS, "I1": _DEFAULTS, "F1": _DEFAULTS,
    # Bundesliga: 16 teams, 2 direct relegation + 1 play-off → treat as 3
    "D1": _DEFAULTS,
    # Championship / lower tiers — we don't emphasize European push there
    "E1": {"relegation_from_bottom": 3, "europe_top": 2, "title_top": 2},
    # Catch-all handled via _DEFAULTS.get fallback below
}


@dataclass
class TeamMotivation:
    team_name: str
    rank: int
    played: int
    matches_left: int

    # 0..1 scalars — higher means this stake is more alive
    relegation_risk: float = 0.0
    title_push: float = 0.0
    europe_push: float = 0.0
    dead_rubber: float = 0.0  # 1 means nothing left to play for

    # Summary intensity in [0, 1] used as a lambda multiplier
    intensity: float = 0.0

    # Turkish-language human reasons, shown in UI
    reasons: list[str] = field(default_factory=list)


def compute_team_motivation(
    standings: Standings,
    team_id: int,
) -> TeamMotivation | None:
    """Turn a team's standings row into a motivation profile.

    Returns None if the team isn't in this standings (e.g. wrong league).
    """
    row = standings.row_of(team_id)
    rank = standings.rank_of(team_id)
    if row is None or rank is None:
        return None

    hints = _LEAGUE_POSITION_HINTS.get(standings.league, _DEFAULTS)

    # Per-team "matches left" — we approximate as total_scheduled/team minus played.
    # In round-robin leagues every team plays (N-1)*2 fixtures; when that is
    # unavailable (mid-transition seasons, playoff splits) we fall back to the
    # max games played observed so far, so "left" never goes negative.
    expected_total_per_team = 0
    if standings.total_teams > 1:
        expected_total_per_team = (standings.total_teams - 1) * 2
    max_played = max((r.played for r in standings.rows), default=row.played)
    games_basis = max(expected_total_per_team, max_played)
    matches_left = max(0, games_basis - row.played)

    mot = TeamMotivation(
        team_name=row.team_name,
        rank=rank,
        played=row.played,
        matches_left=matches_left,
    )

    # Nothing decided yet? Bail out with a neutral profile (early season).
    if row.played < 5:
        mot.intensity = 0.0
        return mot

    # --- Relegation risk ---
    # Distance in points from the highest safe position; divide by
    # the max "recoverable" points in the games remaining (3 per match).
    reli = _relegation_risk(standings, rank, row, matches_left, hints)
    mot.relegation_risk = reli

    # --- Title push ---
    tit = _title_push(standings, rank, row, matches_left, hints)
    mot.title_push = tit

    # --- Europe push (only for leagues where it applies) ---
    eur = 0.0
    if hints["europe_top"] > hints["title_top"]:
        eur = _europe_push(standings, rank, row, matches_left, hints)
    mot.europe_push = eur

    # --- Dead rubber ---
    # Nothing to play for: not in any race AND not close to relegation.
    # Only penalize in the last ~20% of the season to avoid false positives
    # in October.
    season_progress = row.played / max(games_basis, 1)
    if (
        season_progress >= 0.75
        and reli < 0.1 and tit < 0.1 and eur < 0.15
    ):
        mot.dead_rubber = min(1.0, (season_progress - 0.75) * 4)  # ramps 0→1 over last 25%

    # --- Reasons (Turkish, user-facing) ---
    if reli >= 0.5:
        mot.reasons.append(
            f"Küme düşme hattına çok yakın ({row.team_name} {rank}. sırada, {matches_left} maç kaldı)"
        )
    elif reli >= 0.2:
        mot.reasons.append(f"Küme hattıyla puan farkı az ({matches_left} maç kaldı)")
    if tit >= 0.5:
        mot.reasons.append(f"Şampiyonluk yarışında ({rank}. sırada, {matches_left} maç kaldı)")
    if eur >= 0.5:
        mot.reasons.append(f"Avrupa kupası hattında yarışıyor ({rank}. sırada)")
    if mot.dead_rubber >= 0.5:
        mot.reasons.append("Sezon biterken oynayacağı bir şey kalmadı (dead rubber)")

    # --- Intensity summary ---
    # Max rather than sum: a single strong stake matters more than two weak ones.
    mot.intensity = max(reli, tit, eur)
    # Dead rubber suppresses intensity
    if mot.dead_rubber > 0:
        mot.intensity = mot.intensity * (1.0 - mot.dead_rubber)

    return mot


def _relegation_risk(
    standings: Standings,
    rank: int,
    row: TeamRow,
    matches_left: int,
    hints: dict[str, int],
) -> float:
    """How threatening the relegation zone is for this team right now."""
    if matches_left == 0:
        return 0.0
    n = standings.total_teams
    reli_cutoff_rank = n - hints["relegation_from_bottom"]  # e.g. 17 in a 20-team league
    if rank > reli_cutoff_rank:
        # Already in the zone — risk is a function of how far from safety.
        safe_row = standings.rows[reli_cutoff_rank - 1]  # rank index → 0-based
        gap = safe_row.points - row.points
        # More games left = still escapable, but we want "risk" to stay high
        # as long as relegation is mathematically on the table.
        max_points_left = 3 * matches_left
        if max_points_left <= 0:
            return 1.0
        # Risk scales with gap but never exceeds 1.
        return min(1.0, 0.6 + 0.4 * (gap / max(max_points_left, 1)))
    # Above the cut: look at how close the drop is behind them.
    zone_row = standings.rows[reli_cutoff_rank]  # first team in the zone (0-based)
    gap_above = row.points - zone_row.points
    max_points_left = 3 * matches_left
    if gap_above >= max_points_left:
        return 0.0  # mathematically safe
    return max(0.0, 1.0 - gap_above / max(max_points_left, 1))


def _title_push(
    standings: Standings,
    rank: int,
    row: TeamRow,
    matches_left: int,
    hints: dict[str, int],
) -> float:
    """How alive the title race is for this team."""
    if matches_left == 0:
        return 0.0
    if rank > hints["title_top"] + 2:
        # More than 4th in a top-2 race → realistically out.
        return 0.0
    leader = standings.rows[0]
    gap = leader.points - row.points
    if rank == 1:
        # Leader: high push unless runner-up is far behind.
        if len(standings.rows) < 2:
            return 1.0
        runner = standings.rows[1]
        runner_gap = row.points - runner.points
        max_points_left = 3 * matches_left
        if runner_gap >= max_points_left:
            return 0.2  # mathematically champion — coast
        return min(1.0, 0.7 + 0.3 * (1 - runner_gap / max(max_points_left, 1)))
    max_points_left = 3 * matches_left
    if gap >= max_points_left:
        return 0.0
    return max(0.0, 1.0 - gap / max(max_points_left, 1))


def _europe_push(
    standings: Standings,
    rank: int,
    row: TeamRow,
    matches_left: int,
    hints: dict[str, int],
) -> float:
    """How alive the battle for a European spot is."""
    if matches_left == 0:
        return 0.0
    cutoff = hints["europe_top"]
    max_points_left = 3 * matches_left
    if rank <= cutoff:
        # Inside the zone — risk of dropping out.
        if len(standings.rows) <= cutoff:
            return 0.3
        first_out = standings.rows[cutoff]  # 0-based → cutoff+1 th place
        gap = row.points - first_out.points
        if gap >= max_points_left:
            return 0.1  # comfortably in
        return min(1.0, 0.5 + 0.5 * (1 - gap / max(max_points_left, 1)))
    # Outside — can we chase up?
    if rank > cutoff + 3:
        return 0.0
    last_in = standings.rows[cutoff - 1]
    gap = last_in.points - row.points
    if gap >= max_points_left:
        return 0.0
    return max(0.0, 1.0 - gap / max(max_points_left, 1))
