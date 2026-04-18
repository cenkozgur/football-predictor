/**
 * Display names for football-data.co.uk league codes.
 *
 * Mirrors the `NAME`s defined in backend/app/ingestion/leagues.py. If we add
 * a league on the backend we should mirror it here too — we deliberately
 * don't fetch this from the backend because the mapping is tiny and static.
 */

export const LEAGUE_NAMES: Record<string, { name: string; country: string }> = {
  // Main format
  E0: { name: "Premier League", country: "England" },
  D1: { name: "Bundesliga", country: "Germany" },
  I1: { name: "Serie A", country: "Italy" },
  SP1: { name: "La Liga", country: "Spain" },
  F1: { name: "Ligue 1", country: "France" },
  N1: { name: "Eredivisie", country: "Netherlands" },
  B1: { name: "Jupiler Pro League", country: "Belgium" },
  P1: { name: "Primeira Liga", country: "Portugal" },
  T1: { name: "Süper Lig", country: "Türkiye" },
  G1: { name: "Super League", country: "Greece" },
  SC0: { name: "Premiership", country: "Scotland" },
  // New format
  AUT: { name: "Bundesliga", country: "Austria" },
  DNK: { name: "Superliga", country: "Denmark" },
  FIN: { name: "Veikkausliiga", country: "Finland" },
  IRL: { name: "Premier Division", country: "Ireland" },
  NOR: { name: "Eliteserien", country: "Norway" },
  POL: { name: "Ekstraklasa", country: "Poland" },
  ROU: { name: "Liga I", country: "Romania" },
  RUS: { name: "Premier League", country: "Russia" },
  SWE: { name: "Allsvenskan", country: "Sweden" },
  SWZ: { name: "Super League", country: "Switzerland" },
};

export function leagueDisplay(code: string): string {
  const entry = LEAGUE_NAMES[code];
  return entry ? `${entry.country} · ${entry.name}` : code;
}
