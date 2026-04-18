/**
 * Formatting helpers for match and prediction display.
 *
 * All prediction probabilities arrive as floats in [0, 1] — we render them
 * as percentages with no decimal by default, because any finer granularity
 * is implying precision the model doesn't have.
 */

export function pct(p: number, decimals = 0): string {
  return `${(p * 100).toFixed(decimals)}%`;
}

export function fmtKickoff(iso: string): string {
  // Backend stores naive UTC (SQLite quirk). Treat the string as UTC
  // regardless of whether it has a Z suffix.
  const normalized = iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z";
  const d = new Date(normalized);
  return d.toLocaleString("tr-TR", {
    weekday: "short",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/Istanbul",
  });
}

export function fmtKickoffDate(iso: string): string {
  const normalized = iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z";
  const d = new Date(normalized);
  return d.toLocaleDateString("tr-TR", {
    weekday: "long",
    day: "2-digit",
    month: "long",
    year: "numeric",
    timeZone: "Europe/Istanbul",
  });
}
