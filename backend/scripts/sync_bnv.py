"""Bugün Ne Var nightly sync.

Mirrors the logic of `Bugün Ne Var`'s in-browser /seed page server-side
so users don't have to manually open it every morning. Pulls fixtures
from the same sources the browser does:

  - Football: api.cenkozgur.com/matches (this very backend)
  - Basketball (NBA only): api.cenkozgur.com/sport-events?sport=basketball
  - F1:  https://api.jolpi.ca/ergast/f1/<year>/next.json
  - MotoGP / Moto2 / Moto3 / WSBK: static 2026 calendar (mirror of
    dataSources.js)
  - TV events: small hand-curated list

…and upserts them into Base44 via its REST API. Static rosters
(football clubs, NBA franchises, volleyball clubs, tennis players) are
also seeded so onboarding has selectable entries even before any
fixture is in flight.

Auth: a single `api_key` header. The probe on 2026-04-30 confirmed
GET / POST / PUT / DELETE all return 200 with this scheme. Key lives
in BASE44_API_KEY env var.

Run:
    BASE44_API_KEY=... python -m scripts.sync_bnv

GitHub Actions:
    secrets: BASE44_API_KEY
    invoked from .github/workflows/daily-ingest.yml after the football
    and basketball ingest steps so the data we read from /matches and
    /sport-events is fresh.

Idempotency: dedupe by external_ref. Re-running is a no-op except for
events whose start_time / status changed. Old events (>2 days past)
get pruned at the start of each run.
"""
from __future__ import annotations

import os
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


# ──────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────

BASE44_APP_ID = os.environ.get("BASE44_APP_ID", "69ebd11fe74b0ffcc2427b1b")
BASE44_API = f"https://app.base44.com/api/apps/{BASE44_APP_ID}/entities"
FOOTBALL_PREDICTOR_API = os.environ.get(
    "FOOTBALL_PREDICTOR_API", "https://api.cenkozgur.com"
)
JOLPICA_BASE = "https://api.jolpi.ca/ergast/f1"
SEASON_YEAR = 2026

# Base44 rate-limit dodge: per-request sleep + a longer breath every N
# writes. Same constants the browser /seed uses, scaled a touch larger
# because cron gets one shot and we'd rather take 60s than have a write
# silently dropped.
WRITE_SLEEP_S = 0.15
BREATH_EVERY_N = 15
BREATH_SLEEP_S = 1.0

# Football window — keep aligned with dataSources.js
FOOTBALL_WINDOW_DAYS = 14
NBA_WINDOW_DAYS = 14


def _client() -> httpx.Client:
    api_key = os.environ.get("BASE44_API_KEY")
    if not api_key:
        print("BASE44_API_KEY env var is required.", file=sys.stderr)
        sys.exit(1)
    return httpx.Client(
        headers={"api_key": api_key, "Content-Type": "application/json"},
        timeout=30.0,
    )


def _request_with_retry(method, *args, **kwargs):
    """Wrap an httpx call with exponential backoff on 429.

    Base44 occasionally rate-limits bursts even with our per-write
    sleeps. Two retries with a 2s / 5s backoff cleared every 429
    observed in the 2026-04-30 dry run."""
    last_exc = None
    for attempt, delay in enumerate((0, 2.0, 5.0)):
        if delay:
            time.sleep(delay)
        try:
            r = method(*args, **kwargs)
            if r.status_code == 429:
                last_exc = httpx.HTTPStatusError(
                    f"429 on attempt {attempt + 1}",
                    request=r.request, response=r,
                )
                continue
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                last_exc = exc
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("unreachable")


# ──────────────────────────────────────────────────────────────────────
# League / team mappings (mirror of dataSources.js)
# ──────────────────────────────────────────────────────────────────────

LEAGUE_LABELS = {
    "T1":  "🇹🇷 Süper Lig",
    "E0":  "🇬🇧 Premier League",
    "E1":  "🇬🇧 Championship",
    "SP1": "🇪🇸 La Liga",
    "I1":  "🇮🇹 Serie A",
    "D1":  "🇩🇪 Bundesliga",
    "F1":  "🇫🇷 Ligue 1",
    "N1":  "🇳🇱 Eredivisie",
    "P1":  "🇵🇹 Primeira Liga",
}

LEAGUE_BROADCASTERS = {
    "T1":  "beIN Sports HD 1",
    "E0":  "S Sport / S Sport Plus",
    "E1":  "S Sport",
    "SP1": "S Sport",
    "I1":  "S Sport",
    "D1":  "S Sport / S Sport Plus",
    "F1":  "S Sport",
    "N1":  "S Sport",
    "P1":  "S Sport",
}

# ASCII upstream → display name. Mirrors TEAM_DISPLAY_OVERRIDES in
# dataSources.js. Slug stays computed off the raw name so refs are
# identical regardless of which spelling the source uses.
TEAM_DISPLAY_OVERRIDES = {
    "Besiktas": "Beşiktaş",
    "Fenerbahce": "Fenerbahçe",
    "Goztep": "Göztepe",
    "Kasimpasa": "Kasımpaşa",
    "Eyupspor": "Eyüpspor",
    "Gaziantep": "Gaziantep FK",
    "Basaksehir": "Başakşehir",
    "Nott'm Forest": "Nottingham Forest",
    "Man United": "Manchester United",
    "Man City": "Manchester City",
    "Wolves": "Wolverhampton",
    "Sociedad": "Real Sociedad",
    "Vallecano": "Rayo Vallecano",
    "Sp Lisbon": "Sporting Lisbon",
    "Sp Braga": "Sporting Braga",
    "M'gladbach": "Borussia M'gladbach",
    "Paris SG": "Paris Saint-Germain",
}


def display_name(raw: str) -> str:
    return TEAM_DISPLAY_OVERRIDES.get(raw, raw)


def _slugify(text: str) -> str:
    nfkd = unicodedata.normalize("NFD", text)
    ascii_only = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    out = []
    last_dash = False
    for c in ascii_only.lower():
        if c.isalnum():
            out.append(c)
            last_dash = False
        else:
            if not last_dash:
                out.append("-")
                last_dash = True
    return "".join(out).strip("-")


def team_ref(league: str, name: str) -> str:
    if not name:
        return ""
    return f"team:{league}:{_slugify(name)}"


def competition_ref(league: str) -> str:
    return f"league:{league}"


# Static competitions seeded regardless of fixture availability so
# onboarding always has the umbrella entries to subscribe to.
STATIC_COMPETITIONS = [
    # (external_ref, display_name, category_slug)
    ("league:nba",          "🇺🇸 NBA",                              "nba"),
    ("league:euroleague",   "🇪🇺 EuroLeague",                       "nba"),
    ("league:bsl",          "🇹🇷 Basketbol Süper Ligi",             "nba"),
    ("league:tr_volleyball","🇹🇷 Voleybol (Sultanlar + Efeler)",    "voleybol"),
    ("tour:atp_wta",        "🎾 ATP / WTA Tour",                    "tenis"),
    ("series:f1:2026",      "🏎 Formula 1 2026",                    "f1"),
    ("series:motogp:2026",  "🏍 MotoGP 2026",                       "motogp"),
    ("series:moto2:2026",   "🏍 Moto2 2026",                        "motogp"),
    ("series:moto3:2026",   "🏍 Moto3 2026",                        "motogp"),
    ("series:wsbk:2026",    "🏍 WorldSBK 2026",                     "motogp"),
    ("tv:turkiye",          "📺 Türkiye TV Etkinlikleri",           "tv"),
]

# Static rosters — mirror dataSources.STATIC_LEAGUE_ROSTERS. Only the
# leagues with real fixture coverage are listed here for football; NBA /
# tennis players / volleyball clubs are seeded separately below.
FOOTBALL_ROSTERS: dict[str, list[tuple[str, str]]] = {
    "T1": [
        ("Galatasaray", "Galatasaray"), ("Fenerbahçe", "Fenerbahce"),
        ("Beşiktaş", "Besiktas"), ("Trabzonspor", "Trabzonspor"),
        ("Başakşehir", "Basaksehir"), ("Adana Demirspor", "Adana Demirspor"),
        ("Antalyaspor", "Antalyaspor"), ("Konyaspor", "Konyaspor"),
        ("Kasımpaşa", "Kasimpasa"), ("Alanyaspor", "Alanyaspor"),
        ("Sivasspor", "Sivasspor"), ("Kayserispor", "Kayserispor"),
        ("Rizespor", "Rizespor"), ("Samsunspor", "Samsunspor"),
        ("Eyüpspor", "Eyupspor"), ("Göztepe", "Goztep"),
        ("Gaziantep FK", "Gaziantep"), ("Kocaelispor", "Kocaelispor"),
    ],
    "E0": [
        ("Liverpool", "Liverpool"), ("Arsenal", "Arsenal"),
        ("Manchester City", "Man City"), ("Manchester United", "Man United"),
        ("Chelsea", "Chelsea"), ("Tottenham", "Tottenham"),
        ("Newcastle", "Newcastle"), ("Aston Villa", "Aston Villa"),
        ("Brighton", "Brighton"), ("West Ham", "West Ham"),
        ("Crystal Palace", "Crystal Palace"), ("Brentford", "Brentford"),
        ("Fulham", "Fulham"), ("Wolves", "Wolves"),
        ("Everton", "Everton"), ("Bournemouth", "Bournemouth"),
        ("Nottingham Forest", "Nott'm Forest"), ("Leeds", "Leeds"),
        ("Burnley", "Burnley"), ("Sunderland", "Sunderland"),
    ],
    "SP1": [
        ("Real Madrid", "Real Madrid"), ("Barcelona", "Barcelona"),
        ("Atletico Madrid", "Ath Madrid"), ("Athletic Bilbao", "Ath Bilbao"),
        ("Real Sociedad", "Sociedad"), ("Real Betis", "Betis"),
        ("Sevilla", "Sevilla"), ("Villarreal", "Villarreal"),
        ("Valencia", "Valencia"), ("Celta Vigo", "Celta"),
        ("Getafe", "Getafe"), ("Osasuna", "Osasuna"),
        ("Mallorca", "Mallorca"), ("Girona", "Girona"),
        ("Espanyol", "Espanyol"), ("Rayo Vallecano", "Vallecano"),
        ("Alaves", "Alaves"), ("Levante", "Levante"),
        ("Real Oviedo", "Oviedo"), ("Elche", "Elche"),
    ],
    "I1": [
        ("Inter", "Inter"), ("Juventus", "Juventus"),
        ("Milan", "Milan"), ("Napoli", "Napoli"),
        ("Roma", "Roma"), ("Lazio", "Lazio"),
        ("Atalanta", "Atalanta"), ("Fiorentina", "Fiorentina"),
        ("Bologna", "Bologna"), ("Torino", "Torino"),
        ("Udinese", "Udinese"), ("Genoa", "Genoa"),
        ("Cagliari", "Cagliari"), ("Lecce", "Lecce"),
        ("Hellas Verona", "Verona"), ("Parma", "Parma"),
        ("Como", "Como"), ("Pisa", "Pisa"),
        ("Cremonese", "Cremonese"), ("Sassuolo", "Sassuolo"),
    ],
    "D1": [
        ("Bayern München", "Bayern Munich"), ("Borussia Dortmund", "Dortmund"),
        ("RB Leipzig", "RB Leipzig"), ("Bayer Leverkusen", "Leverkusen"),
        ("Eintracht Frankfurt", "Ein Frankfurt"), ("VfB Stuttgart", "Stuttgart"),
        ("Werder Bremen", "Werder Bremen"), ("Wolfsburg", "Wolfsburg"),
        ("Hoffenheim", "Hoffenheim"), ("Mainz", "Mainz"),
        ("Augsburg", "Augsburg"), ("Freiburg", "Freiburg"),
        ("Borussia M'gladbach", "M'gladbach"), ("Union Berlin", "Union Berlin"),
        ("Heidenheim", "Heidenheim"), ("St Pauli", "St Pauli"),
        ("Köln", "Koln"), ("Hamburger SV", "Hamburg"),
    ],
    "F1": [
        ("Paris Saint-Germain", "Paris SG"), ("Marseille", "Marseille"),
        ("Monaco", "Monaco"), ("Lyon", "Lyon"),
        ("Lille", "Lille"), ("Nice", "Nice"),
        ("Rennes", "Rennes"), ("Lens", "Lens"),
        ("Strasbourg", "Strasbourg"), ("Toulouse", "Toulouse"),
        ("Brest", "Brest"), ("Nantes", "Nantes"),
        ("Auxerre", "Auxerre"), ("Le Havre", "Le Havre"),
        ("Angers", "Angers"), ("Metz", "Metz"),
        ("Lorient", "Lorient"), ("Paris FC", "Paris FC"),
    ],
    "N1": [
        ("Ajax", "Ajax"), ("PSV Eindhoven", "PSV Eindhoven"),
        ("Feyenoord", "Feyenoord"), ("AZ Alkmaar", "AZ Alkmaar"),
        ("FC Twente", "Twente"), ("FC Utrecht", "Utrecht"),
        ("SC Heerenveen", "Heerenveen"), ("NEC Nijmegen", "NEC Nijmegen"),
        ("Sparta Rotterdam", "Sparta Rotterdam"), ("Go Ahead Eagles", "Go Ahead Eagles"),
    ],
    "P1": [
        ("Benfica", "Benfica"), ("Porto", "Porto"),
        ("Sporting Lisbon", "Sp Lisbon"), ("Sporting Braga", "Sp Braga"),
        ("Vitoria Guimaraes", "Guimaraes"), ("Famalicao", "Famalicao"),
        ("Rio Ave", "Rio Ave"), ("Estoril", "Estoril"),
        ("Moreirense", "Moreirense"), ("Casa Pia", "Casa Pia"),
    ],
}

NBA_TEAMS = [
    ("Boston Celtics", "BOS"), ("Brooklyn Nets", "BKN"),
    ("New York Knicks", "NYK"), ("Philadelphia 76ers", "PHI"),
    ("Toronto Raptors", "TOR"), ("Chicago Bulls", "CHI"),
    ("Cleveland Cavaliers", "CLE"), ("Detroit Pistons", "DET"),
    ("Indiana Pacers", "IND"), ("Milwaukee Bucks", "MIL"),
    ("Atlanta Hawks", "ATL"), ("Charlotte Hornets", "CHA"),
    ("Miami Heat", "MIA"), ("Orlando Magic", "ORL"),
    ("Washington Wizards", "WAS"),
    ("Denver Nuggets", "DEN"), ("Minnesota Timberwolves", "MIN"),
    ("Oklahoma City Thunder", "OKC"), ("Portland Trail Blazers", "POR"),
    ("Utah Jazz", "UTA"), ("Golden State Warriors", "GSW"),
    ("LA Clippers", "LAC"), ("Los Angeles Lakers", "LAL"),
    ("Phoenix Suns", "PHX"), ("Sacramento Kings", "SAC"),
    ("Dallas Mavericks", "DAL"), ("Houston Rockets", "HOU"),
    ("Memphis Grizzlies", "MEM"), ("New Orleans Pelicans", "NOP"),
    ("San Antonio Spurs", "SAS"),
]

EUROLEAGUE_TEAMS = [
    ("Anadolu Efes", "EFES"), ("Fenerbahçe Beko", "FBB"),
    ("Real Madrid", "RMB"), ("FC Barcelona", "BAR_B"),
    ("Olympiacos", "OLY"), ("Panathinaikos AKTOR", "PAO"),
    ("Maccabi Tel Aviv", "MAC"), ("Žalgiris Kaunas", "ZAL"),
    ("Crvena zvezda", "CZV"), ("Partizan", "PAR"),
    ("Olimpia Milano", "MIL_B"), ("Virtus Bologna", "VIRT"),
    ("ASVEL", "ASV"), ("AS Monaco", "MON_B"),
    ("Paris Basketball", "PAR_B"), ("Bayern München", "BAY_B"),
    ("ALBA Berlin", "ALBA"), ("Baskonia", "BAS"),
]

BSL_TEAMS = [
    ("Anadolu Efes", "BSL_EFES"), ("Fenerbahçe Beko", "BSL_FBB"),
    ("Galatasaray MCT Technic", "BSL_GS"), ("Beşiktaş Fibabanka", "BSL_BJK"),
    ("TOFAŞ", "BSL_TOFAS"), ("Bahçeşehir Koleji", "BSL_BAH"),
    ("Türk Telekom", "BSL_TTEL"), ("Pınar Karşıyaka", "BSL_KSK"),
    ("Aliağa Petkimspor", "BSL_PETKIM"), ("Manisa Büyükşehir Belediye", "BSL_MAN"),
    ("Bandırma B.İ.K.", "BSL_BAND"), ("Yukatel Merkezefendi", "BSL_MERK"),
    ("Mersin Spor", "BSL_MER"), ("Onvo Büyükçekmece Basketbol", "BSL_BCEK"),
    ("Samsunspor", "BSL_SAM"), ("Esenler Erokspor", "BSL_ESN"),
]

VOLLEYBALL_TEAMS = [
    ("VakıfBank", "VBK_W"), ("Eczacıbaşı Dynavit", "ECZ_W"),
    ("Fenerbahçe Opet", "FB_W"), ("Galatasaray Daikin", "GS_W"),
    ("Türk Hava Yolları", "THY_W"), ("Beşiktaş Kadın", "BJK_W"),
    ("Halkbank", "HALK_M"), ("Ziraat Bankkart", "ZB_M"),
    ("Fenerbahçe HDI", "FB_M"), ("Galatasaray HDI", "GS_M"),
    ("Arkasspor", "ARK_M"), ("Tokat Belediye Plevne", "TOKAT_M"),
]

TENNIS_PLAYERS = [
    ("Jannik Sinner", "sinner"), ("Carlos Alcaraz", "alcaraz"),
    ("Novak Djokovic", "djokovic"), ("Daniil Medvedev", "medvedev"),
    ("Alexander Zverev", "zverev"), ("Stefanos Tsitsipas", "tsitsipas"),
    ("Andrey Rublev", "rublev"), ("Holger Rune", "rune"),
    ("Taylor Fritz", "fritz"), ("Casper Ruud", "ruud"),
    ("Iga Świątek", "swiatek"), ("Aryna Sabalenka", "sabalenka"),
    ("Coco Gauff", "gauff"), ("Elena Rybakina", "rybakina"),
    ("Jessica Pegula", "pegula"), ("Ons Jabeur", "jabeur"),
    ("Madison Keys", "keys"), ("Qinwen Zheng", "zheng"),
    ("Jasmine Paolini", "paolini"), ("Mirra Andreeva", "andreeva"),
]


# MotoGP / Moto2 / Moto3 race calendar — mirror of dataSources.js.
# Times are typical TR broadcast slots (Europe/Istanbul = UTC+3).
MOTOGP_2026 = [
    (1,  "Tayland GP",     "Chang Uluslararası",  "Tayland",     "2026-03-01"),
    (2,  "Brezilya GP",    "Autódromo Ayrton Senna", "Brezilya", "2026-03-22"),
    (3,  "Amerika GP",     "Circuit of the Americas", "ABD",     "2026-03-29"),
    (4,  "İspanya GP",     "Jerez",               "İspanya",     "2026-04-26"),
    (5,  "Fransa GP",      "Le Mans",             "Fransa",      "2026-05-10"),
    (6,  "Katalonya GP",   "Barcelona-Catalunya", "İspanya",     "2026-05-17"),
    (7,  "İtalya GP",      "Mugello",             "İtalya",      "2026-05-31"),
    (8,  "Macaristan GP",  "Balaton Park",        "Macaristan",  "2026-06-07"),
    (9,  "Çekya GP",       "Brno",                "Çekya",       "2026-06-21"),
    (10, "Hollanda GP",    "TT Circuit Assen",    "Hollanda",    "2026-06-28"),
    (11, "Almanya GP",     "Sachsenring",         "Almanya",     "2026-07-12"),
    (12, "Britanya GP",    "Silverstone",         "Birleşik Krallık", "2026-08-09"),
    (13, "Aragon GP",      "MotorLand Aragón",    "İspanya",     "2026-08-30"),
    (14, "San Marino GP",  "Misano",              "San Marino",  "2026-09-13"),
    (15, "Avusturya GP",   "Red Bull Ring",       "Avusturya",   "2026-09-20"),
    (16, "Japonya GP",     "Motegi",              "Japonya",     "2026-10-04"),
    (17, "Endonezya GP",   "Mandalika",           "Endonezya",   "2026-10-11"),
    (18, "Avustralya GP",  "Phillip Island",      "Avustralya",  "2026-10-25"),
    (19, "Malezya GP",     "Sepang",              "Malezya",     "2026-11-01"),
    (20, "Katar GP",       "Lusail",              "Katar",       "2026-11-08"),
    (21, "Portekiz GP",    "Portimão",            "Portekiz",    "2026-11-22"),
    (22, "Valencia GP",    "Ricardo Tormo",       "İspanya",     "2026-11-29"),
]

MOTO_CLASSES = [
    {"slug": "motogp", "label": "MotoGP", "hour": 14, "broadcaster": "S Sport 2"},
    {"slug": "moto2",  "label": "Moto2",  "hour": 12, "broadcaster": "S Sport 2"},
    {"slug": "moto3",  "label": "Moto3",  "hour": 11, "broadcaster": "S Sport 2"},
]

WSBK_2026 = [
    (1,  "Avustralya",      "Phillip Island",        "Avustralya",  "2026-02-22"),
    (2,  "Portekiz",        "Algarve",               "Portekiz",    "2026-03-29"),
    (3,  "Hollanda",        "TT Circuit Assen",      "Hollanda",    "2026-04-19"),
    (4,  "Macaristan",      "Balaton Park",          "Macaristan",  "2026-05-03"),
    (5,  "Çekya",           "Autodrom Most",         "Çekya",       "2026-05-17"),
    (6,  "Aragón",          "MotorLand Aragón",      "İspanya",     "2026-05-31"),
    (7,  "Emilia-Romagna",  "Misano",                "İtalya",      "2026-06-14"),
    (8,  "Birleşik Krallık","Donington Park",        "Birleşik Krallık", "2026-07-12"),
    (9,  "Fransa",          "Magny-Cours",           "Fransa",      "2026-09-06"),
    (10, "İtalya",          "Cremona",               "İtalya",      "2026-09-27"),
    (11, "Estoril",         "Estoril",               "Portekiz",    "2026-10-11"),
    (12, "İspanya",         "Jerez",                 "İspanya",     "2026-10-18"),
]

WSBK_SESSIONS = [
    {"key": "Race1",     "label": "Yarış 1",         "day_offset": -1, "hour": 14},
    {"key": "Superpole", "label": "Superpole Yarış", "day_offset":  0, "hour": 13},
    {"key": "Race2",     "label": "Yarış 2",         "day_offset":  0, "hour": 16},
]


# ──────────────────────────────────────────────────────────────────────
# Source fetchers — return dicts with our `_competition_ref`,
# `_home_entity_ref`, etc. fields. Identical contract to
# dataSources.js.
# ──────────────────────────────────────────────────────────────────────

def fetch_football(session: httpx.Client) -> list[dict[str, Any]]:
    cutoff = datetime.now(tz=timezone.utc) + timedelta(days=FOOTBALL_WINDOW_DAYS)
    r = session.get(
        f"{FOOTBALL_PREDICTOR_API}/matches",
        params={"upcoming": "true", "limit": 200},
    )
    r.raise_for_status()
    matches = r.json()
    out = []
    for m in matches:
        try:
            kickoff = datetime.fromisoformat(m["kickoff"].replace("Z", "+00:00"))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        if kickoff > cutoff:
            continue
        league = m.get("league") or ""
        home = display_name(m.get("home_team", ""))
        away = display_name(m.get("away_team", ""))
        is_live = m.get("status") in {"in_play", "paused"}
        comp_name = LEAGUE_LABELS.get(league, league)
        out.append({
            "title": f"{home} – {away}",
            "competition_name": comp_name,
            "start_time": kickoff.isoformat(),
            "broadcaster": LEAGUE_BROADCASTERS.get(league, ""),
            "venue": "",
            "is_live": is_live,
            "live_status": f"{m['live_minute']}'" if m.get("live_minute") else "",
            "_category_slug": "futbol",
            "_source_id": f"football:{m.get('id')}",
            "_competition_ref": competition_ref(league),
            "_competition_name": comp_name,
            "_home_entity_ref": team_ref(league, m.get("home_team", "")),
            "_home_entity_name": home,
            "_away_entity_ref": team_ref(league, m.get("away_team", "")),
            "_away_entity_name": away,
        })
    return out


def fetch_basketball(session: httpx.Client) -> list[dict[str, Any]]:
    """NBA only via /sport-events?sport=basketball. EuroLeague + BSL
    aren't ingested upstream yet (api-sports free tier was years out of
    date, ESPN doesn't cover them) — those leagues stay roster-only on
    the Bugün Ne Var side."""
    league_display = {
        "NBA":        "🇺🇸 NBA",
        "EuroLeague": "🇪🇺 EuroLeague",
        "BSL":        "🇹🇷 Basketbol Süper Ligi",
    }
    league_refs = {
        "NBA":        ("league:nba",        "nba"),
        "EuroLeague": ("league:euroleague", "el"),
        "BSL":        ("league:bsl",        "bsl"),
    }
    # ESPN team display names → static-roster slug seed. Same map as
    # dataSources.js.
    nba_team_slug = {
        "Boston Celtics": "bos", "Brooklyn Nets": "bkn",
        "New York Knicks": "nyk", "Philadelphia 76ers": "phi",
        "Toronto Raptors": "tor", "Chicago Bulls": "chi",
        "Cleveland Cavaliers": "cle", "Detroit Pistons": "det",
        "Indiana Pacers": "ind", "Milwaukee Bucks": "mil",
        "Atlanta Hawks": "atl", "Charlotte Hornets": "cha",
        "Miami Heat": "mia", "Orlando Magic": "orl",
        "Washington Wizards": "was",
        "Denver Nuggets": "den", "Minnesota Timberwolves": "min",
        "Oklahoma City Thunder": "okc", "Portland Trail Blazers": "por",
        "Utah Jazz": "uta", "Golden State Warriors": "gsw",
        "LA Clippers": "lac", "Los Angeles Clippers": "lac",
        "Los Angeles Lakers": "lal",
        "Phoenix Suns": "phx", "Sacramento Kings": "sac",
        "Dallas Mavericks": "dal", "Houston Rockets": "hou",
        "Memphis Grizzlies": "mem", "New Orleans Pelicans": "nop",
        "San Antonio Spurs": "sas",
    }

    r = session.get(
        f"{FOOTBALL_PREDICTOR_API}/sport-events",
        params={"sport": "basketball", "upcoming": "true", "limit": 300},
    )
    r.raise_for_status()
    games = r.json()
    out = []
    for g in games:
        league_code = g.get("league") or "NBA"
        comp_ref, ts = league_refs.get(league_code, ("league:nba", "nba"))
        comp_name = league_display.get(league_code, league_code)
        home_raw = g.get("home_team", "")
        away_raw = g.get("away_team", "") or ""
        home_slug = nba_team_slug.get(home_raw) or _slugify(home_raw)
        away_slug = nba_team_slug.get(away_raw) or _slugify(away_raw)
        out.append({
            "title": f"{home_raw} – {away_raw}",
            "competition_name": comp_name,
            "start_time": g.get("kickoff"),
            "broadcaster": g.get("broadcaster") or "",
            "venue": g.get("venue") or "",
            "is_live": g.get("status") == "in_play",
            "_category_slug": "nba",
            "_source_id": g.get("external_ref"),
            "_competition_ref": comp_ref,
            "_competition_name": comp_name,
            "_home_entity_ref": f"team:{ts}:{home_slug}",
            "_home_entity_name": home_raw,
            "_away_entity_ref": f"team:{ts}:{away_slug}",
            "_away_entity_name": away_raw,
        })
    return out


def fetch_f1(session: httpx.Client) -> list[dict[str, Any]]:
    r = session.get(f"{JOLPICA_BASE}/{SEASON_YEAR}/next.json")
    r.raise_for_status()
    data = r.json()
    races = data.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    if not races:
        return []
    race = races[0]
    venue = (race.get("Circuit") or {}).get("circuitName", "")
    grand_prix = race.get("raceName", "Grand Prix")
    comp_ref = "series:f1:2026"
    comp_name = "🏎 Formula 1 2026"
    broadcaster = "S Sport / S Sport 2"

    sessions: list[dict[str, Any]] = []

    def add(session_key: str, label: str, date_str: str, time_str: str) -> None:
        if not date_str or not time_str:
            return
        try:
            dt = datetime.fromisoformat(f"{date_str}T{time_str}".replace("Z", "+00:00"))
        except ValueError:
            return
        sessions.append({
            "title": f"{grand_prix} — {label}",
            "competition_name": comp_name,
            "start_time": dt.isoformat(),
            "broadcaster": broadcaster,
            "venue": venue,
            "is_live": False,
            "_category_slug": "f1",
            "_source_id": f"f1:{race.get('season')}:{race.get('round')}:{session_key}",
            "_competition_ref": comp_ref,
            "_competition_name": comp_name,
        })

    if race.get("FirstPractice"):
        fp = race["FirstPractice"]
        add("FP1", "Antrenman 1", fp.get("date"), fp.get("time"))
    if race.get("SecondPractice"):
        fp = race["SecondPractice"]
        add("FP2", "Antrenman 2", fp.get("date"), fp.get("time"))
    if race.get("SprintQualifying"):
        sq = race["SprintQualifying"]
        add("SprintQuali", "Sprint Sıralama", sq.get("date"), sq.get("time"))
    if race.get("ThirdPractice"):
        fp = race["ThirdPractice"]
        add("FP3", "Antrenman 3", fp.get("date"), fp.get("time"))
    if race.get("Sprint"):
        sp = race["Sprint"]
        add("Sprint", "Sprint", sp.get("date"), sp.get("time"))
    if race.get("Qualifying"):
        q = race["Qualifying"]
        add("Quali", "Sıralama", q.get("date"), q.get("time"))
    add("Race", "Yarış", race.get("date"), race.get("time"))
    return sessions


def build_motogp_events() -> list[dict[str, Any]]:
    out = []
    for round_num, gp_name, circuit, country, date_str in MOTOGP_2026:
        for cls in MOTO_CLASSES:
            iso = f"{date_str}T{cls['hour']:02d}:00:00+03:00"
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError:
                continue
            comp_name = f"🏍 {cls['label']} 2026"
            out.append({
                "title": f"{gp_name} — {cls['label']} Yarışı",
                "competition_name": comp_name,
                "start_time": dt.isoformat(),
                "broadcaster": cls["broadcaster"],
                "venue": f"{circuit}, {country}",
                "is_live": False,
                "_category_slug": "motogp",
                "_source_id": f"{cls['slug']}:2026:{round_num}:Race",
                "_competition_ref": f"series:{cls['slug']}:2026",
                "_competition_name": comp_name,
            })
    return out


def build_wsbk_events() -> list[dict[str, Any]]:
    out = []
    for round_num, name, circuit, country, date_str in WSBK_2026:
        try:
            sunday = datetime.fromisoformat(f"{date_str}T00:00:00+03:00")
        except ValueError:
            continue
        for s in WSBK_SESSIONS:
            dt = sunday + timedelta(days=s["day_offset"], hours=s["hour"])
            out.append({
                "title": f"{name} — {s['label']}",
                "competition_name": "🏍 WorldSBK 2026",
                "start_time": dt.isoformat(),
                "broadcaster": "S Sport",
                "venue": f"{circuit}, {country}",
                "is_live": False,
                "_category_slug": "motogp",
                "_source_id": f"wsbk:2026:{round_num}:{s['key']}",
                "_competition_ref": "series:wsbk:2026",
                "_competition_name": "🏍 WorldSBK 2026",
            })
    return out


def build_tv_events() -> list[dict[str, Any]]:
    """Hand-curated TR TV events. Refresh manually as new must-watch
    broadcasts are announced."""
    today = datetime.now(tz=timezone.utc).astimezone(timezone(timedelta(hours=3)))
    today = today.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    return [
        {
            "title": "Survivor All Star — Eleme Gecesi",
            "competition_name": "📺 Türkiye TV Etkinlikleri",
            "start_time": today.replace(hour=22).isoformat(),
            "broadcaster": "TV8",
            "is_live": False,
            "_category_slug": "tv",
            "_source_id": "tv:survivor:today",
            "_competition_ref": "tv:turkiye",
            "_competition_name": "📺 Türkiye TV Etkinlikleri",
        },
        {
            "title": "MasterChef Türkiye",
            "competition_name": "📺 Türkiye TV Etkinlikleri",
            "start_time": tomorrow.replace(hour=20).isoformat(),
            "broadcaster": "TV8",
            "is_live": False,
            "_category_slug": "tv",
            "_source_id": "tv:masterchef:tomorrow",
            "_competition_ref": "tv:turkiye",
            "_competition_name": "📺 Türkiye TV Etkinlikleri",
        },
    ]


def build_static_team_seeds() -> list[dict[str, Any]]:
    """Roster of every selectable team/player across categories. Each
    entry has only `_entity_*` and `_competition_ref` (no event)."""
    out = []
    for league, roster in FOOTBALL_ROSTERS.items():
        for name, slug_seed in roster:
            out.append({
                "_category_slug": "futbol",
                "_entity_name": name,
                "_entity_ref": team_ref(league, slug_seed),
                "_competition_ref": competition_ref(league),
                "_entity_type": "team",
            })
    for name, code in NBA_TEAMS:
        out.append({
            "_category_slug": "nba",
            "_entity_name": name,
            "_entity_ref": f"team:nba:{code.lower()}",
            "_competition_ref": "league:nba",
            "_entity_type": "team",
        })
    for name, code in EUROLEAGUE_TEAMS:
        out.append({
            "_category_slug": "nba",
            "_entity_name": name,
            "_entity_ref": f"team:el:{code.lower()}",
            "_competition_ref": "league:euroleague",
            "_entity_type": "team",
        })
    for name, code in BSL_TEAMS:
        out.append({
            "_category_slug": "nba",
            "_entity_name": name,
            "_entity_ref": f"team:bsl:{code.lower()}",
            "_competition_ref": "league:bsl",
            "_entity_type": "team",
        })
    for name, code in VOLLEYBALL_TEAMS:
        out.append({
            "_category_slug": "voleybol",
            "_entity_name": name,
            "_entity_ref": f"team:tr_vb:{code.lower()}",
            "_competition_ref": "league:tr_volleyball",
            "_entity_type": "team",
        })
    for name, slug in TENNIS_PLAYERS:
        out.append({
            "_category_slug": "tenis",
            "_entity_name": name,
            "_entity_ref": f"player:atp_wta:{slug}",
            "_competition_ref": "tour:atp_wta",
            "_entity_type": "player",
        })
    return out


# ──────────────────────────────────────────────────────────────────────
# Base44 REST helpers
# ──────────────────────────────────────────────────────────────────────

def _list_all(client: httpx.Client, entity: str) -> list[dict[str, Any]]:
    r = _request_with_retry(client.get, f"{BASE44_API}/{entity}")
    return r.json()


def _create(client: httpx.Client, entity: str, body: dict[str, Any]) -> dict[str, Any]:
    r = _request_with_retry(client.post, f"{BASE44_API}/{entity}", json=body)
    return r.json()


def _update(client: httpx.Client, entity: str, row_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    r = _request_with_retry(client.put, f"{BASE44_API}/{entity}/{row_id}", json=patch)
    return r.json()


def _delete(client: httpx.Client, entity: str, row_id: str) -> None:
    _request_with_retry(client.delete, f"{BASE44_API}/{entity}/{row_id}")


def _throttle(idx: int) -> None:
    time.sleep(WRITE_SLEEP_S)
    if idx > 0 and idx % BREATH_EVERY_N == 0:
        time.sleep(BREATH_SLEEP_S)


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"[sync_bnv] starting at {datetime.now(tz=timezone.utc).isoformat()}")

    with _client() as client:
        # 1. Fetch all sources
        try:
            football = fetch_football(client)
            print(f"  ⚽ {len(football)} football events")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! football fetch failed: {exc}")
            football = []

        try:
            basketball = fetch_basketball(client)
            print(f"  🏀 {len(basketball)} basketball events")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! basketball fetch failed: {exc}")
            basketball = []

        try:
            f1 = fetch_f1(client)
            print(f"  🏎 {len(f1)} F1 sessions")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! F1 fetch failed: {exc}")
            f1 = []

        now = datetime.now(tz=timezone.utc)
        moto = [
            e for e in build_motogp_events()
            if datetime.fromisoformat(e["start_time"]) > now
        ]
        wsbk = [
            e for e in build_wsbk_events()
            if datetime.fromisoformat(e["start_time"]) > now
        ]
        tv = build_tv_events()
        print(f"  🏍 {len(moto)} MotoGP/Moto2/Moto3 + {len(wsbk)} WSBK")
        print(f"  📺 {len(tv)} TV")

        collected = football + basketball + f1 + moto + wsbk + tv
        if not collected:
            print("[sync_bnv] no sources produced rows; exiting")
            return

        # 2. Resolve categories
        categories = _list_all(client, "Category")
        cat_by_slug = {c.get("slug"): c for c in categories if c.get("slug")}
        for slug in ("futbol", "f1", "motogp", "nba", "tenis", "voleybol", "tv"):
            if slug not in cat_by_slug:
                print(f"  ! missing Category slug={slug}")

        # 3. Cleanup + dedupe Competition / TrackedEntity
        existing_comps = _list_all(client, "Competition")
        # Drop legacy comps without external_ref (residue from earlier
        # experiments) so the picker isn't full of dupes.
        cleaned_c = 0
        for c in existing_comps:
            if not c.get("external_ref"):
                try:
                    _delete(client, "Competition", c["id"])
                    cleaned_c += 1
                    _throttle(cleaned_c)
                except httpx.HTTPError:
                    pass
        if cleaned_c:
            print(f"  cleaned {cleaned_c} legacy Competition rows")
            existing_comps = _list_all(client, "Competition")
        comp_by_ref: dict[str, dict[str, Any]] = {
            c["external_ref"]: c for c in existing_comps if c.get("external_ref")
        }

        existing_ents = _list_all(client, "TrackedEntity")
        cleaned_e = 0
        for e in existing_ents:
            if not e.get("external_ref"):
                try:
                    _delete(client, "TrackedEntity", e["id"])
                    cleaned_e += 1
                    _throttle(cleaned_e)
                except httpx.HTTPError:
                    pass
        if cleaned_e:
            print(f"  cleaned {cleaned_e} legacy TrackedEntity rows")
            existing_ents = _list_all(client, "TrackedEntity")
        ent_by_ref: dict[str, dict[str, Any]] = {
            e["external_ref"]: e for e in existing_ents if e.get("external_ref")
        }

        # 4. Build wantComps (static + fixture-derived)
        want_comps: dict[str, dict[str, Any]] = {}
        for ref, name, slug in STATIC_COMPETITIONS:
            want_comps[ref] = {"name": name, "category_slug": slug}
        for seed in collected:
            ref = seed.get("_competition_ref")
            if not ref or ref in want_comps:
                continue
            want_comps[ref] = {
                "name": seed.get("_competition_name") or seed.get("competition_name", ""),
                "category_slug": seed.get("_category_slug", "futbol"),
            }

        comp_created = comp_updated = 0
        for idx, (ref, spec) in enumerate(want_comps.items()):
            cat = cat_by_slug.get(spec["category_slug"])
            if not cat:
                continue
            existing = comp_by_ref.get(ref)
            try:
                if existing is None:
                    created = _create(client, "Competition", {
                        "name": spec["name"],
                        "category_id": cat["id"],
                        "external_ref": ref,
                    })
                    comp_by_ref[ref] = created
                    comp_created += 1
                elif existing.get("name") != spec["name"]:
                    _update(client, "Competition", existing["id"], {"name": spec["name"]})
                    existing["name"] = spec["name"]
                    comp_updated += 1
                else:
                    continue
                _throttle(idx)
            except httpx.HTTPError as exc:
                print(f"  ! Competition {spec['name']}: {exc}")
        print(f"  Competitions: +{comp_created} new, ~{comp_updated} updated, "
              f"={len(comp_by_ref) - comp_created} kept")

        # 5. Build wantEnts (static rosters + fixture-derived home/away)
        want_ents: dict[str, dict[str, Any]] = {}
        for seed in collected:
            for side in ("home", "away"):
                ref = seed.get(f"_{side}_entity_ref")
                name = seed.get(f"_{side}_entity_name")
                if not ref or not name or ref in want_ents:
                    continue
                want_ents[ref] = {
                    "name": name,
                    "category_slug": seed.get("_category_slug", "futbol"),
                    "type": "team",
                    "competition_ref": seed.get("_competition_ref", ""),
                }
        for st in build_static_team_seeds():
            ref = st["_entity_ref"]
            if ref in want_ents:
                continue
            want_ents[ref] = {
                "name": st["_entity_name"],
                "category_slug": st["_category_slug"],
                "type": st.get("_entity_type", "team"),
                "competition_ref": st.get("_competition_ref", ""),
            }

        ent_created = ent_updated = 0
        for idx, (ref, spec) in enumerate(want_ents.items()):
            cat = cat_by_slug.get(spec["category_slug"])
            if not cat:
                continue
            existing = ent_by_ref.get(ref)
            try:
                if existing is None:
                    created = _create(client, "TrackedEntity", {
                        "name": spec["name"],
                        "category_id": cat["id"],
                        "type": spec["type"],
                        "external_ref": ref,
                        "competition_ref": spec["competition_ref"],
                    })
                    ent_by_ref[ref] = created
                    ent_created += 1
                else:
                    patch: dict[str, Any] = {}
                    if spec["competition_ref"] and existing.get("competition_ref") != spec["competition_ref"]:
                        patch["competition_ref"] = spec["competition_ref"]
                    if spec["name"] and existing.get("name") != spec["name"]:
                        patch["name"] = spec["name"]
                    if not patch:
                        continue
                    _update(client, "TrackedEntity", existing["id"], patch)
                    ent_updated += 1
                _throttle(idx)
            except httpx.HTTPError as exc:
                print(f"  ! TrackedEntity {spec['name']}: {exc}")
        print(f"  TrackedEntities: +{ent_created} new, ~{ent_updated} updated, "
              f"={len(ent_by_ref) - ent_created} kept")

        # 6. Events: prune stale + upcoming-window dupes, then upsert
        all_events = _list_all(client, "Event")
        cutoff_past = (datetime.now(tz=timezone.utc) - timedelta(days=2)).timestamp() * 1000
        cutoff_future = (datetime.now(tz=timezone.utc) + timedelta(days=7)).timestamp() * 1000
        incoming_refs = {e["_source_id"] for e in collected if e.get("_source_id")}

        existing_by_ref = {e.get("external_ref"): e for e in all_events if e.get("external_ref")}

        to_delete = []
        for e in all_events:
            try:
                t = datetime.fromisoformat(e["start_time"].replace("Z", "+00:00")).timestamp() * 1000
            except (KeyError, ValueError):
                continue
            is_stale = t < cutoff_past
            is_overwriting = bool(e.get("external_ref")) and e["external_ref"] in incoming_refs and existing_by_ref.get(e["external_ref"]) is e
            is_legacy_demo = (
                not e.get("external_ref")
                and t >= cutoff_past
                and t <= cutoff_future
            )
            if is_stale or is_legacy_demo:
                to_delete.append(e)

        for idx, ev in enumerate(to_delete):
            try:
                _delete(client, "Event", ev["id"])
                _throttle(idx)
            except httpx.HTTPError:
                pass
        print(f"  pruned {len(to_delete)} stale/legacy Event rows")

        # Upsert events: by external_ref → update if exists, else create
        existing_by_ref = {
            e.get("external_ref"): e
            for e in _list_all(client, "Event")
            if e.get("external_ref")
        }

        ev_created = ev_updated = ev_skipped = 0
        for idx, seed in enumerate(collected):
            ref = seed.get("_source_id")
            if not ref:
                ev_skipped += 1
                continue
            cat = cat_by_slug.get(seed.get("_category_slug"))
            if not cat:
                ev_skipped += 1
                continue

            payload = {
                "title": seed.get("title", ""),
                "competition_name": seed.get("competition_name", ""),
                "start_time": seed.get("start_time"),
                "broadcaster": seed.get("broadcaster") or "",
                "venue": seed.get("venue") or "",
                "is_live": bool(seed.get("is_live")),
                "live_status": seed.get("live_status") or "",
                "category_id": cat["id"],
                "external_ref": ref,
            }
            if seed.get("_competition_ref"):
                payload["competition_ref"] = seed["_competition_ref"]
                comp = comp_by_ref.get(seed["_competition_ref"])
                if comp:
                    payload["competition_id"] = comp["id"]
            if seed.get("_home_entity_ref"):
                payload["home_entity_ref"] = seed["_home_entity_ref"]
            if seed.get("_away_entity_ref"):
                payload["away_entity_ref"] = seed["_away_entity_ref"]

            existing = existing_by_ref.get(ref)
            try:
                if existing is None:
                    _create(client, "Event", payload)
                    ev_created += 1
                else:
                    # Only patch fields that diverge.
                    patch: dict[str, Any] = {}
                    for k, v in payload.items():
                        if existing.get(k) != v:
                            patch[k] = v
                    if not patch:
                        continue
                    _update(client, "Event", existing["id"], patch)
                    ev_updated += 1
                _throttle(idx)
            except httpx.HTTPError as exc:
                print(f"  ! Event {seed.get('title')}: {exc}")
                ev_skipped += 1

        print(f"  Events: +{ev_created} new, ~{ev_updated} updated, "
              f"!{ev_skipped} skipped")
        print(f"[sync_bnv] done at {datetime.now(tz=timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
