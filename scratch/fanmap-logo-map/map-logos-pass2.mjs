// Pass 2: resolve the clubs pass 1 couldn't match.
// Better matcher (city-name synonyms, token subsets, edit distance) over
// per-country team lists, then /teams?search= fallback for stragglers.
// Inputs: teams.json + pass1/report.json + pass1/logo-map.json (artifact of pass 1).
import fs from "fs";

const KEY = process.env.API_FOOTBALL_KEY;
if (!KEY) { console.error("API_FOOTBALL_KEY missing"); process.exit(1); }
const BASE = "https://v3.football.api-sports.io";
const OUT_DIR = process.env.OUT_DIR || ".";
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function api(path) {
  for (let attempt = 1; attempt <= 6; attempt++) {
    const res = await fetch(BASE + path, { headers: { "x-apisports-key": KEY } });
    if (res.status === 429) { await sleep(20000 * attempt); continue; }
    if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
    const body = await res.json();
    const errs = body.errors && Object.keys(body.errors).length ? body.errors : null;
    if (errs?.rateLimit || errs?.requests) { await sleep(20000 * attempt); continue; }
    if (errs) throw new Error(`${path} -> ${JSON.stringify(errs)}`);
    await sleep(6500);
    return body;
  }
  throw new Error(`${path} -> rate limited after retries`);
}

const SYN = {
  wien: "vienna", munchen: "munich", koln: "cologne", praha: "prague",
  warszawa: "warsaw", lisboa: "lisbon", bucuresti: "bucharest",
  beograd: "belgrade", moskva: "moscow", sankt: "saint", st: "saint",
};
const norm = s => (s || "")
  .toLowerCase()
  .normalize("NFD").replace(/[̀-ͯ]/g, "")
  .replace(/ı/g, "i").replace(/ß/g, "ss")
  .replace(/[^a-z0-9]+/g, " ")
  .trim();
const STOP = new Set(["fc","cf","afc","cfc","sc","ac","fk","sk","bk","if","ca","cd","sd","ud","us","ss","as","sv","svg","vfb","vfl","fsv","tsv","tsg","bsc","nk","hnk","gnk","pfc","kv","krc","kaa","rsc","club","cp","de","1","2","royal","sporting","sport","atletico","athletic","cs","cr","ec","se","rj","sp"]);
const tokens = s => norm(s).split(" ").filter(Boolean).map(t => SYN[t] || t);
const strip = s => tokens(s).filter(t => !STOP.has(t)).join(" ").trim();

function lev(a, b) {
  if (Math.abs(a.length - b.length) > 2) return 99;
  const dp = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
  for (let j = 1; j <= b.length; j++) dp[0][j] = j;
  for (let i = 1; i <= a.length; i++)
    for (let j = 1; j <= b.length; j++)
      dp[i][j] = Math.min(dp[i-1][j] + 1, dp[i][j-1] + 1, dp[i-1][j-1] + (a[i-1] === b[j-1] ? 0 : 1));
  return dp[a.length][b.length];
}

function bestMatch(ourName, candidates) {
  const s = strip(ourName), toks = new Set(s.split(" ").filter(Boolean));
  if (!s) return null;
  let best = null, bestScore = 0;
  for (const c of candidates) {
    const cs = strip(c.name);
    if (!cs) continue;
    let score = 0;
    if (cs === s) score = 100;
    else if (lev(cs, s) <= 2 && s.length >= 5) score = 90;
    else if ((cs.includes(s) || s.includes(cs)) && Math.min(cs.length, s.length) >= 4) score = 80;
    else {
      const ctoks = new Set(cs.split(" "));
      const inter = [...toks].filter(t => ctoks.has(t));
      const smaller = Math.min(toks.size, ctoks.size);
      if (smaller > 0 && inter.length === smaller) score = 60 + inter.length * 5;   // full subset
      else if (inter.length && inter.some(t => t.length >= 6)) score = 40 + inter.length * 5; // distinctive shared token
    }
    if (score > bestScore) { best = c; bestScore = score; }
    else if (score === bestScore && score > 0 && best && c.id !== best.id) best = { ...best, tie: true };
  }
  if (!best || bestScore < 40 || best.tie) return null;
  return { ...best, score: bestScore };
}

const teams = JSON.parse(fs.readFileSync(new URL("./teams.json", import.meta.url), "utf8"));
const countryOf = Object.fromEntries(teams.map(t => [t.name, t.country]));
const report1 = JSON.parse(fs.readFileSync("pass1/report.json", "utf8"));
const map1 = JSON.parse(fs.readFileSync("pass1/logo-map.json", "utf8"));

const unmatched = report1.unmatched.map(k => {
  const i = k.lastIndexOf("@");
  return { name: k.slice(0, i), country: k.slice(i + 1) };
});

// Country name resolution (same aliases as pass 1).
const apiCountries = (await api("/countries")).response.map(c => c.name);
const ALIASES = {
  "DR Congo": ["Congo DR", "Congo-DR", "Congo"], "UAE": ["United Arab Emirates"],
  "USA": ["USA", "United States"], "Republic of Ireland": ["Ireland"],
  "Turkey": ["Turkey", "Türkiye"], "South Korea": ["South Korea", "Korea Republic"],
  "Czech Republic": ["Czech Republic", "Czechia"],
};
const apiByNorm = Object.fromEntries(apiCountries.map(c => [norm(c), c]));
const resolveCountry = ours => {
  for (const cand of [ours, ...(ALIASES[ours] || [])]) if (apiByNorm[norm(cand)]) return apiByNorm[norm(cand)];
  const n = norm(ours);
  return apiCountries.find(c => norm(c).includes(n) || n.includes(norm(c))) || null;
};

const map2 = {}, stillUnmatched = [];
const byCountry = {};
for (const u of unmatched) (byCountry[u.country] ||= []).push(u.name);

// Round A: refetch each affected country's list, apply the better matcher.
for (const country of Object.keys(byCountry).sort()) {
  const apiCountry = resolveCountry(country);
  if (!apiCountry) { stillUnmatched.push(...byCountry[country].map(n => ({ name: n, country }))); continue; }
  const body = await api(`/teams?country=${encodeURIComponent(apiCountry)}`);
  const apiTeams = body.response.map(r => ({ id: r.team.id, name: r.team.name, logo: r.team.logo }));
  for (const ours of byCountry[country]) {
    const hit = bestMatch(ours, apiTeams);
    if (hit) map2[`${ours}@${country}`] = { id: hit.id, logo: hit.logo, apiName: hit.name, method: `pass2-country-${hit.score}` };
    else stillUnmatched.push({ name: ours, country });
  }
  console.log(`${country}: ${byCountry[country].length} retried, ${byCountry[country].length - stillUnmatched.filter(x => x.country === country).length} recovered`);
}

// Round B: per-club search for the rest.
const finalUnmatched = [];
for (const { name, country } of stillUnmatched) {
  const term = strip(name).slice(0, 30).trim() || norm(name);
  if (term.length < 3) { finalUnmatched.push(`${name}@${country}`); continue; }
  let cands = [];
  try {
    const body = await api(`/teams?search=${encodeURIComponent(term)}`);
    cands = body.response.map(r => ({ id: r.team.id, name: r.team.name, logo: r.team.logo, country: r.team.country }));
  } catch (e) { console.log(`search failed for ${name}: ${e.message}`); }
  const apiCountry = resolveCountry(country);
  const inCountry = cands.filter(c => !apiCountry || c.country === apiCountry);
  const hit = bestMatch(name, inCountry.length ? inCountry : []);
  if (hit) map2[`${name}@${country}`] = { id: hit.id, logo: hit.logo, apiName: hit.name, method: `pass2-search-${hit.score}` };
  else { finalUnmatched.push(`${name}@${country}`); console.log(`STILL UNMATCHED: ${name}@${country} (term "${term}", ${cands.length} results)`); }
}

const merged = { ...map1, ...map2 };
fs.writeFileSync(`${OUT_DIR}/logo-map.json`, JSON.stringify(merged, null, 1));
fs.writeFileSync(`${OUT_DIR}/report.json`, JSON.stringify({
  total: teams.length, matched: Object.keys(merged).length,
  pass2Recovered: Object.keys(map2).length, unmatched: finalUnmatched,
}, null, 1));
console.log(`DONE pass2: +${Object.keys(map2).length} recovered, total ${Object.keys(merged).length}/${teams.length}, still unmatched ${finalUnmatched.length}`);
