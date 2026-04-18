/**
 * Typed client for the Football Predictor FastAPI backend.
 *
 * Every function is called from Server Components (async pages) so the browser
 * never talks to the backend directly — Next.js fetches on the server and
 * streams the rendered HTML. `cache: "no-store"` keeps predictions fresh on
 * each request; swap to time-based revalidation later when we have real traffic.
 */

const API_BASE =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export interface MatchOut {
  id: number;
  league: string;
  season: string;
  kickoff: string;
  home_team: string;
  away_team: string;
  status: string;
  ft_home: number | null;
  ft_away: number | null;
}

export interface MatchContext {
  id: number;
  league: string;
  season: string;
  kickoff: string;
  home_team: string;
  away_team: string;
  status: string;
}

export type MarketMap = Record<string, number>;

export interface CorrectScoreEntry {
  [score: string]: number;
}

export interface PredictionPayload {
  "1X2": MarketMap;
  double_chance: MarketMap;
  over_under: Record<string, MarketMap>;
  btts: MarketMap;
  odd_even: MarketMap;
  goal_range: MarketMap;
  correct_score_top10: CorrectScoreEntry[];
  asian_handicap_0: MarketMap;
  "asian_handicap_-0.5": MarketMap;
  "asian_handicap_-1": MarketMap;
  "asian_handicap_+0.5": MarketMap;
  "asian_handicap_+1": MarketMap;
  "home_over_under_1.5"?: MarketMap;
  "away_over_under_1.5"?: MarketMap;
  [key: string]: unknown;
}

export interface PredictionView {
  match: MatchContext;
  model_version: string;
  generated_at: string;
  lambda_home: number;
  lambda_away: number;
  payload: PredictionPayload;
}

async function apiFetch<T>(path: string): Promise<T> {
  const url = `${API_BASE}${path}`;
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`API ${res.status} ${res.statusText} at ${path}`);
  }
  return (await res.json()) as T;
}

export async function listMatches(params?: {
  league?: string;
  upcoming?: boolean;
  limit?: number;
}): Promise<MatchOut[]> {
  const search = new URLSearchParams();
  if (params?.league) search.set("league", params.league);
  if (params?.upcoming !== undefined)
    search.set("upcoming", String(params.upcoming));
  if (params?.limit !== undefined) search.set("limit", String(params.limit));
  const qs = search.toString();
  return apiFetch<MatchOut[]>(`/matches${qs ? `?${qs}` : ""}`);
}

export async function getMatch(id: number): Promise<MatchOut> {
  return apiFetch<MatchOut>(`/matches/${id}`);
}

export interface CouponLeg {
  match_id: number;
  home_team: string;
  away_team: string;
  kickoff: string;
  league: string;
  market: string;
  market_label: string;
  selection: string;
  selection_label: string;
  prob: number;
}

export interface Coupon {
  legs: CouponLeg[];
  combined_prob: number;
  num_legs: number;
}

export interface CouponResponse {
  primary: Coupon | null;
  alternatives: Coupon[];
  bankos: Coupon[];
  all_picks: CouponLeg[];
  filters: {
    min_prob_per_leg: number;
    num_legs: number;
    allowed_markets: string[] | null;
  };
  counts: {
    matches_considered: number;
    qualifying_picks: number;
  };
}

export async function getCoupons(params?: {
  min_prob?: number;
  legs?: number;
  markets?: string;
}): Promise<CouponResponse> {
  const search = new URLSearchParams();
  if (params?.min_prob !== undefined)
    search.set("min_prob", String(params.min_prob));
  if (params?.legs !== undefined) search.set("legs", String(params.legs));
  if (params?.markets) search.set("markets", params.markets);
  const qs = search.toString();
  return apiFetch<CouponResponse>(`/coupons${qs ? `?${qs}` : ""}`);
}

export async function getPrediction(id: number): Promise<PredictionView | null> {
  try {
    return await apiFetch<PredictionView>(`/predictions/${id}`);
  } catch (err) {
    // 404 → no prediction written yet for this match; the UI treats that as
    // "model couldn't score this fixture" (e.g. unknown team).
    if (err instanceof Error && err.message.includes("404")) return null;
    throw err;
  }
}
