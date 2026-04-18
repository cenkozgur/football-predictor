import Link from "next/link";
import { notFound } from "next/navigation";
import {
  getMatch,
  getPrediction,
  type MarketMap,
  type MatchOut,
  type PredictionView,
} from "@/lib/api";
import { fmtKickoffDate, pct } from "@/lib/format";
import { leagueDisplay } from "@/lib/leagues";

export const dynamic = "force-dynamic";

export default async function MatchDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id: idStr } = await params;
  const id = Number(idStr);
  if (!Number.isFinite(id)) notFound();

  let match: MatchOut | null = null;
  let fetchError: string | null = null;
  let prediction: PredictionView | null = null;
  try {
    [match, prediction] = await Promise.all([getMatch(id), getPrediction(id)]);
  } catch (err) {
    fetchError = err instanceof Error ? err.message : String(err);
  }

  if (fetchError) {
    return (
      <div className="rounded-2xl border border-red-900/30 bg-red-950/20 p-5">
        <h2 className="font-semibold text-red-400">Yuklenemedi</h2>
        <p className="mt-1 text-sm text-red-400/70">{fetchError}</p>
      </div>
    );
  }
  if (!match) notFound();

  return (
    <div className="space-y-4">
      {/* Back link */}
      <Link
        href="/matches"
        className="inline-flex items-center gap-1 text-xs text-muted hover:text-foreground transition-colors"
      >
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" />
        </svg>
        Maclar
      </Link>

      {/* Match header card */}
      <div className="rounded-2xl border border-card-border bg-card p-5">
        <div className="text-[11px] text-muted font-medium uppercase tracking-wider mb-4">
          {leagueDisplay(match.league)}
        </div>
        <div className="flex items-center justify-between">
          <div className="flex-1 text-center">
            <div className="text-lg font-bold leading-tight">{match.home_team}</div>
            <div className="text-[10px] text-muted mt-0.5 uppercase">Ev</div>
          </div>
          <div className="px-4">
            <div className="text-2xl font-bold text-muted">vs</div>
          </div>
          <div className="flex-1 text-center">
            <div className="text-lg font-bold leading-tight">{match.away_team}</div>
            <div className="text-[10px] text-muted mt-0.5 uppercase">Dep</div>
          </div>
        </div>
        <div className="text-center mt-4">
          <span className="text-xs text-muted">{fmtKickoffDate(match.kickoff)}</span>
        </div>
      </div>

      {prediction ? (
        <PredictionSurface prediction={prediction} match={match} />
      ) : (
        <div className="rounded-2xl border border-amber-900/30 bg-amber-950/20 p-5">
          <h2 className="font-semibold text-amber-400">Tahmin hazir degil</h2>
          <p className="mt-1 text-sm text-amber-400/70">
            Bu mac icin model tahmini henuz uretilemedi.
          </p>
        </div>
      )}
    </div>
  );
}

/* ────────────────────────── Prediction Surface ────────────────────────── */

function PredictionSurface({
  prediction,
  match,
}: {
  prediction: PredictionView;
  match: MatchOut;
}) {
  const p = prediction.payload;

  return (
    <div className="space-y-3">
      {/* 1X2 - Primary */}
      <OneXTwoCard market={p["1X2"]} home={match.home_team} away={match.away_team} />

      {/* Double Chance + BTTS + Odd/Even */}
      <div className="grid grid-cols-1 gap-3">
        <MarketCard title="Cifte Sans">
          <ThreeWayPills
            items={[
              { label: "1X", value: p.double_chance["1X"] },
              { label: "12", value: p.double_chance["12"] },
              { label: "X2", value: p.double_chance["X2"] },
            ]}
          />
        </MarketCard>

        <div className="grid grid-cols-2 gap-3">
          <MarketCard title="KG Var/Yok">
            <TwoWayPills
              left={{ label: "Var", value: p.btts.yes }}
              right={{ label: "Yok", value: p.btts.no }}
            />
          </MarketCard>
          <MarketCard title="Tek/Cift">
            <TwoWayPills
              left={{ label: "Tek", value: p.odd_even.odd }}
              right={{ label: "Cift", value: p.odd_even.even }}
            />
          </MarketCard>
        </div>
      </div>

      {/* Over/Under */}
      <MarketCard title="Alt / Ust">
        <div className="space-y-2">
          {Object.entries(p.over_under).map(([line, ou]) => (
            <OverUnderRow key={line} line={line} over={ou.over} under={ou.under} />
          ))}
        </div>
      </MarketCard>

      {/* Goal Range */}
      <MarketCard title="Gol Araligi">
        <div className="grid grid-cols-4 gap-2">
          {Object.entries(p.goal_range).map(([range, prob]) => (
            <div key={range} className="text-center">
              <div className="text-lg font-bold text-accent tabular-nums">{pct(prob)}</div>
              <div className="text-[10px] text-muted mt-0.5">{range}</div>
            </div>
          ))}
        </div>
      </MarketCard>

      {/* Correct Score */}
      <MarketCard title="Kesin Skor">
        <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
          {p.correct_score_top10.map((entry, i) => {
            const [score, prob] = Object.entries(entry)[0];
            const maxProb = Object.values(p.correct_score_top10[0])[0];
            return (
              <div key={`${score}-${i}`} className="flex items-center gap-2">
                <span className="w-4 text-[10px] text-muted tabular-nums">{i + 1}</span>
                <span className="w-8 text-sm font-bold tabular-nums">{score}</span>
                <div className="flex-1 h-1.5 rounded-full bg-white/5 overflow-hidden">
                  <div
                    className="h-full rounded-full bg-accent"
                    style={{ width: `${(prob / maxProb) * 100}%` }}
                  />
                </div>
                <span className="w-11 text-right text-xs text-muted tabular-nums">
                  {pct(prob, 1)}
                </span>
              </div>
            );
          })}
        </div>
      </MarketCard>

      {/* Asian Handicap */}
      <MarketCard title="Asya Handikapi">
        <div className="space-y-2">
          {([
            ["-1", p["asian_handicap_-1"]],
            ["-0.5", p["asian_handicap_-0.5"]],
            ["0", p.asian_handicap_0],
            ["+0.5", p["asian_handicap_+0.5"]],
            ["+1", p["asian_handicap_+1"]],
          ] as [string, MarketMap][]).map(([line, ah]) => (
            <div key={line} className="flex items-center gap-3">
              <span className="w-10 text-xs text-muted tabular-nums text-right">{line}</span>
              <div className="flex-1 flex h-7 rounded-lg overflow-hidden text-xs font-medium">
                <div
                  className="flex items-center justify-center bg-accent/20 text-accent"
                  style={{ width: `${ah.home * 100}%` }}
                >
                  {pct(ah.home)}
                </div>
                <div
                  className="flex items-center justify-center bg-white/5 text-muted"
                  style={{ width: `${ah.away * 100}%` }}
                >
                  {pct(ah.away)}
                </div>
              </div>
            </div>
          ))}
        </div>
        <div className="flex justify-between text-[10px] text-muted mt-2 px-12">
          <span>Ev</span>
          <span>Deplasman</span>
        </div>
      </MarketCard>

      {/* Team Totals */}
      {(p["home_over_under_1.5"] || p["away_over_under_1.5"]) && (
        <div className="grid grid-cols-2 gap-3">
          {p["home_over_under_1.5"] && (
            <MarketCard title={`${match.home_team} 1.5`}>
              <TwoWayPills
                left={{ label: "Ust", value: p["home_over_under_1.5"].over }}
                right={{ label: "Alt", value: p["home_over_under_1.5"].under }}
              />
            </MarketCard>
          )}
          {p["away_over_under_1.5"] && (
            <MarketCard title={`${match.away_team} 1.5`}>
              <TwoWayPills
                left={{ label: "Ust", value: p["away_over_under_1.5"].over }}
                right={{ label: "Alt", value: p["away_over_under_1.5"].under }}
              />
            </MarketCard>
          )}
        </div>
      )}

      {/* Model info */}
      <div className="rounded-xl border border-card-border bg-card/50 p-3 text-[10px] text-muted">
        <div className="flex flex-wrap gap-x-4 gap-y-0.5">
          <span>Model: {prediction.model_version.split("-").slice(0, 3).join("-")}</span>
          <span className="tabular-nums">
            {"\u03BB"} ev: {prediction.lambda_home.toFixed(2)} &middot; dep: {prediction.lambda_away.toFixed(2)}
          </span>
        </div>
      </div>
    </div>
  );
}

/* ────────────────────────── Components ────────────────────────── */

function OneXTwoCard({
  market,
  home,
  away,
}: {
  market: MarketMap;
  home: string;
  away: string;
}) {
  const items = [
    { key: "1", label: home, prob: market["1"] },
    { key: "X", label: "Berabere", prob: market["X"] },
    { key: "2", label: away, prob: market["2"] },
  ];
  const max = Math.max(...items.map((i) => i.prob));

  return (
    <div className="rounded-2xl border border-card-border bg-card p-4">
      <h3 className="text-[11px] font-semibold text-muted uppercase tracking-wider mb-3">
        Mac Sonucu
      </h3>
      <div className="grid grid-cols-3 gap-2">
        {items.map((item) => {
          const isHighest = item.prob === max;
          return (
            <div
              key={item.key}
              className={`rounded-xl p-3 text-center ${
                isHighest
                  ? "bg-accent/15 border border-accent/30"
                  : "bg-white/5 border border-transparent"
              }`}
            >
              <div
                className={`text-2xl font-bold tabular-nums ${
                  isHighest ? "text-accent" : ""
                }`}
              >
                {pct(item.prob)}
              </div>
              <div className="text-[10px] text-muted mt-1 truncate">{item.label}</div>
              <div
                className={`text-xs font-semibold mt-0.5 ${
                  isHighest ? "text-accent" : "text-muted"
                }`}
              >
                {item.key}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function MarketCard({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-2xl border border-card-border bg-card p-4">
      <h3 className="text-[11px] font-semibold text-muted uppercase tracking-wider mb-3">
        {title}
      </h3>
      {children}
    </div>
  );
}

function ThreeWayPills({
  items,
}: {
  items: { label: string; value: number }[];
}) {
  const max = Math.max(...items.map((i) => i.value));
  return (
    <div className="grid grid-cols-3 gap-2">
      {items.map((item) => {
        const isMax = item.value === max;
        return (
          <div
            key={item.label}
            className={`rounded-xl py-2.5 text-center ${
              isMax ? "bg-accent/15" : "bg-white/5"
            }`}
          >
            <div className={`text-base font-bold tabular-nums ${isMax ? "text-accent" : ""}`}>
              {pct(item.value)}
            </div>
            <div className="text-[10px] text-muted mt-0.5">{item.label}</div>
          </div>
        );
      })}
    </div>
  );
}

function TwoWayPills({
  left,
  right,
}: {
  left: { label: string; value: number };
  right: { label: string; value: number };
}) {
  const leftWins = left.value >= right.value;
  return (
    <div className="grid grid-cols-2 gap-2">
      <div className={`rounded-xl py-2.5 text-center ${leftWins ? "bg-accent/15" : "bg-white/5"}`}>
        <div className={`text-base font-bold tabular-nums ${leftWins ? "text-accent" : ""}`}>
          {pct(left.value)}
        </div>
        <div className="text-[10px] text-muted mt-0.5">{left.label}</div>
      </div>
      <div className={`rounded-xl py-2.5 text-center ${!leftWins ? "bg-accent/15" : "bg-white/5"}`}>
        <div className={`text-base font-bold tabular-nums ${!leftWins ? "text-accent" : ""}`}>
          {pct(right.value)}
        </div>
        <div className="text-[10px] text-muted mt-0.5">{right.label}</div>
      </div>
    </div>
  );
}

function OverUnderRow({
  line,
  over,
  under,
}: {
  line: string;
  over: number;
  under: number;
}) {
  const overWins = over > under;
  return (
    <div className="flex items-center gap-2">
      <span className="w-8 text-xs text-muted tabular-nums text-right">{line}</span>
      <div className="flex-1 flex h-8 rounded-lg overflow-hidden text-xs font-medium">
        <div
          className={`flex items-center justify-center ${
            overWins ? "bg-accent/20 text-accent" : "bg-white/5 text-muted"
          }`}
          style={{ width: `${over * 100}%` }}
        >
          Ust {pct(over)}
        </div>
        <div
          className={`flex items-center justify-center ${
            !overWins ? "bg-accent/20 text-accent" : "bg-white/5 text-muted"
          }`}
          style={{ width: `${under * 100}%` }}
        >
          Alt {pct(under)}
        </div>
      </div>
    </div>
  );
}
