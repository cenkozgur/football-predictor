// One-off: map fan-map's 1054 clubs to api-football team IDs (for logos).
// Runs in CI where API_FOOTBALL_KEY is available. Output: logo-map.json + report.
import fs from "fs";

const KEY = process.env.API_FOOTBALL_KEY;
if (!KEY) { console.error("API_FOOTBALL_KEY missing"); process.exit(1); }
const BASE = "https://v3.football.api-sports.io";
const OUT_DIR = process.env.OUT_DIR || ".";

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function api(path) {
  for (let attempt = 1; attempt <= 4; attempt++) {
    const res = await fetch(BASE + path, { headers: { "x-apisports-key": KEY } });
    if (res.status === 429) { await sleep(15000 * attempt); continue; }
    if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
    const body = await res.json();
    if (body.errors && Object.keys(body.errors).length) throw new Error(`${path} -> ${JSON.stringify(body.errors)}`);
    return body;
  }
  throw new Error(`${path} -> rate limited after retries`);
}

const norm = s => (s || "")
  .toLowerCase()
  .normalize("NFD").replace(/[̀-ͯ]/g, "")
  .replace(/ı/g, "i")
  .replace(/[^a-z0-9]+/g, " ")
  .trim();

const STOP = new Set(["fc","cf","afc","cfc","sc","ac","fk","sk","bk","if","ca","cd","sd","ud","us","ss","as","sv","vfb","vfl","fsv","tsv","tsg","bsc","nk","hnk","gnk","pfc","kv","krc","kaa","rsc","club","cp","de","1","2"]);
const strip = s => norm(s).split(" ").filter(t => !STOP.has(t)).join(" ").trim();

const teams = JSON.parse(fs.readFileSync(new URL("./teams.json", import.meta.url), "utf8"));
const byCountry = {};
for (const t of teams) (byCountry[t.country] ||= []).push(t.name);

// Resolve our country names against the API's country list.
const apiCountries = (await api("/countries")).response.map(c => c.name);
const ALIASES = {
  "DR Congo": ["Congo DR", "Congo-DR", "Congo"],
  "UAE": ["United Arab Emirates", "United-Arab-Emirates"],
  "USA": ["USA", "United States"],
  "Republic of Ireland": ["Ireland"],
  "Turkey": ["Turkey", "Türkiye"],
  "South Korea": ["South Korea", "Korea Republic", "South-Korea"],
  "Czech Republic": ["Czech Republic", "Czech-Republic", "Czechia"],
  "Saudi Arabia": ["Saudi Arabia", "Saudi-Arabia"],
  "Costa Rica": ["Costa Rica", "Costa-Rica"],
  "El Salvador": ["El Salvador", "El-Salvador"],
  "New Zealand": ["New Zealand", "New-Zealand"],
  "Northern Ireland": ["Northern Ireland", "Northern-Ireland"],
  "South Africa": ["South Africa", "South-Africa"],
};
const apiByNorm = Object.fromEntries(apiCountries.map(c => [norm(c), c]));
function resolveCountry(ours) {
  for (const cand of [ours, ...(ALIASES[ours] || [])]) {
    const hit = apiByNorm[norm(cand)];
    if (hit) return hit;
  }
  const n = norm(ours);
  const contains = apiCountries.find(c => norm(c).includes(n) || n.includes(norm(c)));
  return contains || null;
}

const logoMap = {};       // "name@country" -> {id, logo, apiName, method}
const unmatched = [];
const countryMisses = [];

for (const country of Object.keys(byCountry).sort()) {
  const apiCountry = resolveCountry(country);
  if (!apiCountry) { countryMisses.push(country); unmatched.push(...byCountry[country].map(n => `${n}@${country}`)); continue; }

  // Fetch every team the API knows in this country (paginated).
  const apiTeams = [];
  let page = 1, totalPages = 1;
  do {
    const body = await api(`/teams?country=${encodeURIComponent(apiCountry)}&page=${page}`);
    apiTeams.push(...body.response.map(r => ({ id: r.team.id, name: r.team.name, logo: r.team.logo })));
    totalPages = body.paging?.total || 1;
    page++;
    await sleep(400);
  } while (page <= totalPages);

  const idx = new Map();  // norm name -> team (first wins)
  const idxStrip = new Map();
  for (const t of apiTeams) {
    const n = norm(t.name), s = strip(t.name);
    if (!idx.has(n)) idx.set(n, t);
    if (s && !idxStrip.has(s)) idxStrip.set(s, t);
  }

  for (const ours of byCountry[country]) {
    const n = norm(ours), s = strip(ours);
    let hit = null, method = null;
    if (idx.has(n)) { hit = idx.get(n); method = "exact"; }
    else if (s && idxStrip.has(s)) { hit = idxStrip.get(s); method = "stripped"; }
    else {
      // containment on stripped names (min length 5, unique hit only)
      const cands = apiTeams.filter(t => {
        const ts = strip(t.name);
        return ts.length >= 5 && s.length >= 5 && (ts.includes(s) || s.includes(ts));
      });
      if (cands.length === 1) { hit = cands[0]; method = "contains"; }
    }
    if (hit) logoMap[`${ours}@${country}`] = { id: hit.id, logo: hit.logo, apiName: hit.name, method };
    else unmatched.push(`${ours}@${country}`);
  }
  console.log(`${country} (${apiCountry}): ${byCountry[country].length} clubs, ${apiTeams.length} api teams, matched ${byCountry[country].length - unmatched.filter(u => u.endsWith("@" + country)).length}`);
}

fs.writeFileSync(`${OUT_DIR}/logo-map.json`, JSON.stringify(logoMap, null, 1));
fs.writeFileSync(`${OUT_DIR}/report.json`, JSON.stringify({
  total: teams.length,
  matched: Object.keys(logoMap).length,
  methodCounts: Object.values(logoMap).reduce((a, v) => (a[v.method] = (a[v.method] || 0) + 1, a), {}),
  countryMisses,
  unmatched,
}, null, 1));
console.log(`DONE: matched ${Object.keys(logoMap).length}/${teams.length}, country misses: ${countryMisses.join(",") || "none"}`);
