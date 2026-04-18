import Link from "next/link";
import { listMatches, type MatchOut } from "@/lib/api";
import { fmtKickoff } from "@/lib/format";
import { leagueDisplay, LEAGUE_NAMES } from "@/lib/leagues";

export const dynamic = "force-dynamic";

export default async function MatchesPage() {
  let matches: MatchOut[] = [];
  let fetchError: string | null = null;
  try {
    matches = await listMatches({ upcoming: true, limit: 200 });
  } catch (err) {
    fetchError = err instanceof Error ? err.message : String(err);
  }

  if (fetchError) {
    return (
      <div className="rounded-2xl border border-red-900/30 bg-red-950/20 p-5">
        <h2 className="font-semibold text-red-400">Maclar yuklenemedi</h2>
        <p className="mt-1 text-sm text-red-400/70">{fetchError}</p>
      </div>
    );
  }

  // Group by date first, then by league within each date
  const byDate = new Map<string, MatchOut[]>();
  for (const m of matches) {
    const dateKey = m.kickoff.split("T")[0];
    if (!byDate.has(dateKey)) byDate.set(dateKey, []);
    byDate.get(dateKey)!.push(m);
  }

  const dateGroups = Array.from(byDate.entries()).sort(([a], [b]) =>
    a.localeCompare(b)
  );

  if (dateGroups.length === 0) {
    return (
      <div className="rounded-2xl border border-card-border bg-card p-6 text-center">
        <div className="text-3xl mb-3">-</div>
        <h2 className="font-semibold">Yaklasan mac yok</h2>
        <p className="mt-2 text-sm text-muted">
          Fiksturleri cekmek icin backend komutlarini calistirin.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-bold tracking-tight">Maclar</h1>
        <p className="text-xs text-muted mt-0.5">
          {matches.length} mac &middot; {dateGroups.length} gun
        </p>
      </div>

      {dateGroups.map(([dateKey, dayMatches]) => {
        const d = new Date(dateKey + "T00:00:00Z");
        const dayLabel = d.toLocaleDateString("tr-TR", {
          weekday: "long",
          day: "numeric",
          month: "long",
          timeZone: "UTC",
        });

        // Group by league within this day
        const byLeague = new Map<string, MatchOut[]>();
        for (const m of dayMatches) {
          if (!byLeague.has(m.league)) byLeague.set(m.league, []);
          byLeague.get(m.league)!.push(m);
        }

        return (
          <section key={dateKey}>
            <div className="sticky top-0 z-10 bg-background/80 backdrop-blur-md py-2 mb-2">
              <h2 className="text-xs font-semibold text-accent uppercase tracking-wider">
                {dayLabel}
              </h2>
            </div>

            <div className="space-y-3">
              {Array.from(byLeague.entries())
                .sort(([a], [b]) => leagueDisplay(a).localeCompare(leagueDisplay(b), "tr"))
                .map(([league, leagueMatches]) => (
                  <div key={league}>
                    <div className="flex items-center gap-2 mb-1.5 px-1">
                      <LeagueFlag code={league} />
                      <span className="text-[11px] text-muted font-medium truncate">
                        {leagueDisplay(league)}
                      </span>
                    </div>
                    <div className="rounded-xl border border-card-border bg-card overflow-hidden divide-y divide-card-border">
                      {leagueMatches.map((m) => (
                        <MatchRow key={m.id} match={m} />
                      ))}
                    </div>
                  </div>
                ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function MatchRow({ match: m }: { match: MatchOut }) {
  return (
    <Link
      href={`/matches/${m.id}`}
      className="flex items-center px-3.5 py-3 active:bg-white/5 transition-colors"
    >
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5">
          <span className="text-sm font-medium truncate">{m.home_team}</span>
        </div>
        <div className="flex items-center gap-1.5 mt-0.5">
          <span className="text-sm font-medium truncate">{m.away_team}</span>
        </div>
      </div>
      <div className="flex items-center gap-3 shrink-0 ml-3">
        <span className="text-xs text-muted tabular-nums">
          {formatTime(m.kickoff)}
        </span>
        <svg className="w-4 h-4 text-muted/50" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" />
        </svg>
      </div>
    </Link>
  );
}

function formatTime(iso: string): string {
  const normalized = iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z";
  const d = new Date(normalized);
  return d.toLocaleString("tr-TR", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/Istanbul",
  });
}

function LeagueFlag({ code }: { code: string }) {
  const entry = LEAGUE_NAMES[code];
  if (!entry) return null;

  const countryFlags: Record<string, string> = {
    England: "gb-eng",
    Germany: "de",
    Italy: "it",
    Spain: "es",
    France: "fr",
    Netherlands: "nl",
    Belgium: "be",
    Portugal: "pt",
    "Turkiye": "tr",
    Greece: "gr",
    Scotland: "gb-sct",
    Austria: "at",
    Denmark: "dk",
    Finland: "fi",
    Ireland: "ie",
    Norway: "no",
    Poland: "pl",
    Romania: "ro",
    Russia: "ru",
    Sweden: "se",
    Switzerland: "ch",
  };

  const flagCode = countryFlags[entry.country];
  if (!flagCode) return null;

  return (
    <img
      src={`https://flagcdn.com/16x12/${flagCode}.png`}
      alt={entry.country}
      width={16}
      height={12}
      className="rounded-[2px] shrink-0"
    />
  );
}
