"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { Coupon, CouponLeg, CouponResponse } from "@/lib/api";

type MarketFilter =
  | "1X2"
  | "double_chance"
  | "btts"
  | "odd_even"
  | "over_under"
  | "correct_score";

const MARKET_OPTIONS: { value: MarketFilter; label: string }[] = [
  { value: "1X2", label: "Mac Sonucu" },
  { value: "double_chance", label: "Cifte Sans" },
  { value: "btts", label: "KG" },
  { value: "over_under", label: "Alt/Ust" },
  { value: "odd_even", label: "Tek/Cift" },
  { value: "correct_score", label: "Kesin Skor" },
];

export default function CouponsClient() {
  const [legs, setLegs] = useState(3);
  const [minProb, setMinProb] = useState(0.65);
  const [markets, setMarkets] = useState<Set<MarketFilter>>(
    new Set(["1X2", "double_chance", "btts"]),
  );
  const [data, setData] = useState<CouponResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    async function fetchCoupons() {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({
          min_prob: String(minProb),
          legs: String(legs),
        });
        if (markets.size > 0) params.set("markets", Array.from(markets).join(","));
        const res = await fetch(`/api/coupons?${params}`, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!res.ok) throw new Error(`API ${res.status}`);
        const json = (await res.json()) as CouponResponse;
        setData(json);
      } catch (err) {
        if (err instanceof Error && err.name !== "AbortError") {
          setError(err.message);
        }
      } finally {
        setLoading(false);
      }
    }
    fetchCoupons();
    return () => controller.abort();
  }, [legs, minProb, markets]);

  const toggleMarket = (m: MarketFilter) => {
    setMarkets((prev) => {
      const next = new Set(prev);
      if (next.has(m)) next.delete(m);
      else next.add(m);
      return next;
    });
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-bold tracking-tight">Kuponlar</h1>
        <p className="text-xs text-muted mt-0.5">
          Modelin en yuksek guvenli secimleri
        </p>
      </div>

      {/* Legs selector */}
      <div className="rounded-2xl border border-card-border bg-card p-4 space-y-4">
        <div>
          <label className="text-[11px] font-semibold text-muted uppercase tracking-wider">
            Mac Sayisi
          </label>
          <div className="grid grid-cols-4 gap-2 mt-2">
            {[1, 2, 3, 4].map((n) => (
              <button
                key={n}
                onClick={() => setLegs(n)}
                className={`rounded-xl py-2.5 text-sm font-semibold transition-colors ${
                  legs === n
                    ? "bg-accent text-black"
                    : "bg-white/5 text-muted active:bg-white/10"
                }`}
              >
                {n === 1 ? "Banko" : `${n}'lu`}
              </button>
            ))}
          </div>
        </div>

        {/* Min probability slider */}
        <div>
          <div className="flex items-center justify-between">
            <label className="text-[11px] font-semibold text-muted uppercase tracking-wider">
              Min Olasilik
            </label>
            <span className="text-sm font-bold text-accent tabular-nums">
              {Math.round(minProb * 100)}%
            </span>
          </div>
          <input
            type="range"
            min={0.5}
            max={0.95}
            step={0.05}
            value={minProb}
            onChange={(e) => setMinProb(Number(e.target.value))}
            className="w-full mt-2 accent-accent"
          />
          <div className="flex justify-between text-[10px] text-muted mt-0.5">
            <span>50%</span>
            <span>95%</span>
          </div>
        </div>

        {/* Market filters */}
        <div>
          <label className="text-[11px] font-semibold text-muted uppercase tracking-wider">
            Marketler
          </label>
          <div className="flex flex-wrap gap-1.5 mt-2">
            {MARKET_OPTIONS.map((opt) => {
              const active = markets.has(opt.value);
              return (
                <button
                  key={opt.value}
                  onClick={() => toggleMarket(opt.value)}
                  className={`rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${
                    active
                      ? "bg-accent/15 text-accent border border-accent/30"
                      : "bg-white/5 text-muted border border-transparent active:bg-white/10"
                  }`}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {error && (
        <div className="rounded-2xl border border-red-900/30 bg-red-950/20 p-4">
          <p className="text-sm text-red-400">{error}</p>
        </div>
      )}

      {loading && !data && (
        <div className="rounded-2xl border border-card-border bg-card p-6 text-center text-sm text-muted">
          Hesaplaniyor...
        </div>
      )}

      {data && (
        <>
          {/* Primary coupon */}
          {data.primary ? (
            <CouponCard coupon={data.primary} highlighted />
          ) : (
            <div className="rounded-2xl border border-amber-900/30 bg-amber-950/20 p-5">
              <h2 className="font-semibold text-amber-400">
                Kriterlere uyan kupon bulunamadi
              </h2>
              <p className="mt-1 text-sm text-amber-400/70">
                {data.counts.qualifying_picks} mac esikleri gecti ama{" "}
                {data.filters.num_legs} bacak icin yeterli degil. Min olasiligi
                dusur veya mac sayisini azalt.
              </p>
            </div>
          )}

          {/* Alternative coupons */}
          {data.alternatives.length > 0 && (
            <div className="space-y-3 pt-2">
              <h2 className="text-[11px] font-semibold text-muted uppercase tracking-wider px-1">
                Alternatifler
              </h2>
              {data.alternatives.map((c, i) => (
                <CouponCard key={i} coupon={c} />
              ))}
            </div>
          )}

          {/* Banko picks */}
          {legs !== 1 && data.bankos.length > 0 && (
            <div className="space-y-2 pt-2">
              <h2 className="text-[11px] font-semibold text-muted uppercase tracking-wider px-1">
                En Guvenli Bankolar
              </h2>
              <div className="rounded-2xl border border-card-border bg-card overflow-hidden divide-y divide-card-border">
                {data.bankos.map((c, i) => (
                  <BankoRow key={i} leg={c.legs[0]} />
                ))}
              </div>
            </div>
          )}

          {/* Disclaimer */}
          <div className="rounded-xl border border-card-border bg-card/50 p-3 mt-4">
            <p className="text-[10px] text-muted leading-relaxed">
              Kuponlar modelin <strong>en guvenli tahmini</strong> olan secimlerdir.
              <br />
              <span className="text-amber-400/80">
                Not: yuksek olasilik = dusuk oran. Bookmaker oranlarinin
                entegrasyonundan sonra "deger" kuponlari da eklenecek.
              </span>
            </p>
          </div>
        </>
      )}
    </div>
  );
}

/* ────────────────────────── Sub-components ────────────────────────── */

function CouponCard({
  coupon,
  highlighted = false,
}: {
  coupon: Coupon;
  highlighted?: boolean;
}) {
  return (
    <div
      className={`rounded-2xl p-4 ${
        highlighted
          ? "border-2 border-accent/40 bg-accent/5"
          : "border border-card-border bg-card"
      }`}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span
            className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded ${
              highlighted
                ? "bg-accent text-black"
                : "bg-white/10 text-muted"
            }`}
          >
            {coupon.num_legs === 1 ? "Banko" : `${coupon.num_legs}'lu Kupon`}
          </span>
        </div>
        <div className="text-right">
          <div className="text-[9px] text-muted uppercase tracking-wider">
            Kazanma
          </div>
          <div
            className={`text-base font-bold tabular-nums ${
              highlighted ? "text-accent" : ""
            }`}
          >
            {(coupon.combined_prob * 100).toFixed(1)}%
          </div>
        </div>
      </div>
      <div className="space-y-2">
        {coupon.legs.map((leg, i) => (
          <LegRow key={i} leg={leg} />
        ))}
      </div>
    </div>
  );
}

function LegRow({ leg }: { leg: CouponLeg }) {
  return (
    <Link
      href={`/matches/${leg.match_id}`}
      className="flex items-center gap-3 p-2.5 rounded-lg bg-white/5 active:bg-white/10 transition-colors"
    >
      <div className="flex-1 min-w-0">
        <div className="text-xs text-muted truncate">
          {leg.home_team} <span className="text-muted/50">vs</span>{" "}
          {leg.away_team}
        </div>
        <div className="text-sm font-semibold mt-0.5 truncate">
          <span className="text-muted font-normal">{leg.market_label}:</span>{" "}
          <span className="text-accent">{formatSelection(leg)}</span>
        </div>
      </div>
      <div className="shrink-0 text-right">
        <div className="text-sm font-bold tabular-nums">
          {(leg.prob * 100).toFixed(0)}%
        </div>
        <div className="text-[9px] text-muted">{formatTime(leg.kickoff)}</div>
      </div>
    </Link>
  );
}

function BankoRow({ leg }: { leg: CouponLeg }) {
  return (
    <Link
      href={`/matches/${leg.match_id}`}
      className="flex items-center px-3.5 py-2.5 active:bg-white/5 transition-colors"
    >
      <div className="flex-1 min-w-0">
        <div className="text-xs text-muted truncate">
          {leg.home_team} <span className="text-muted/50">vs</span>{" "}
          {leg.away_team}
        </div>
        <div className="text-sm font-medium mt-0.5 truncate text-accent">
          {formatSelection(leg)}
        </div>
      </div>
      <div className="shrink-0 text-right ml-3">
        <div className="text-sm font-bold text-accent tabular-nums">
          {(leg.prob * 100).toFixed(0)}%
        </div>
      </div>
    </Link>
  );
}

function formatSelection(leg: CouponLeg): string {
  // Over/Under: extract line from market like "over_under_2.5"
  if (leg.market.startsWith("over_under_")) {
    const line = leg.market.replace("over_under_", "");
    const side = leg.selection === "over" ? "Ust" : "Alt";
    return `${side} ${line}`;
  }
  // Correct score
  if (leg.market === "correct_score") {
    return `Skor ${leg.selection.replace("-", ":")}`;
  }
  // BTTS
  if (leg.market === "btts") {
    return leg.selection === "yes" ? "KG Var" : "KG Yok";
  }
  // Odd/Even
  if (leg.market === "odd_even") {
    return leg.selection === "odd" ? "Tek" : "Cift";
  }
  // 1X2 / Double chance
  if (leg.market === "1X2" || leg.market === "double_chance") {
    return leg.selection;
  }
  return leg.selection_label;
}

function formatTime(iso: string): string {
  const normalized = iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z";
  const d = new Date(normalized);
  return d.toLocaleString("tr-TR", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/Istanbul",
  });
}
