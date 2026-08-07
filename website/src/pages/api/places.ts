import type { APIRoute } from 'astro';
import poisJson from '../../data/pois.json';

// Server-side business search over the Advan-derived POI index. The full
// index never ships to the client; each query returns at most TOP_N matches.
export const prerender = false;

const TOP_N = 8;

interface Poi {
  n: string;
  a: string;
  lat: number;
  lon: number;
}
const POIS: Poi[] = (poisJson as { pois: Poi[] }).pois;

// Advan street suffixes appear both abbreviated ("128 King St") and spelled
// out ("236 KING STREET"), and users type both; canonicalize to the
// abbreviation on both sides.
const SUFFIX: Record<string, string> = {
  street: 'st',
  avenue: 'ave',
  boulevard: 'blvd',
  drive: 'dr',
  road: 'rd',
  lane: 'ln',
  court: 'ct',
  place: 'pl',
  terrace: 'ter',
  highway: 'hwy',
  parkway: 'pkwy',
  square: 'sq',
  plaza: 'plz',
};

// Lowercase, drop apostrophes, break on punctuation, canonicalize suffixes.
function words(s: string): string[] {
  return s
    .toLowerCase()
    .replace(/['’]/g, '')
    .split(/[^a-z0-9]+/)
    .filter(Boolean)
    .map((w) => SUFFIX[w] ?? w);
}

// Every POI is in San Francisco, so locality words carry no signal, and a
// full mailing address ("128 King St, San Francisco, CA 94107") would never
// match the street-only index. Dropped only when other tokens remain, so a
// bare "san francisco" still searches names. "ca" as a stop word costs a
// business literally named "CA ..." that token; acceptable for SF-only data.
const LOCALITY = new Set(['sf', 'ca', 'california', 'usa']);

function queryTokens(q: string): string[] {
  const all = words(q);
  const kept: string[] = [];
  for (let i = 0; i < all.length; i++) {
    if (all[i] === 'san' && all[i + 1] === 'francisco') {
      i++;
      continue;
    }
    if (all[i] === 'united' && all[i + 1] === 'states') {
      i++;
      continue;
    }
    if (LOCALITY.has(all[i])) continue;
    if (/^94\d{3}$/.test(all[i])) continue;
    kept.push(all[i]);
  }
  return kept.length > 0 ? kept : all;
}

// Pre-tokenized copies so each request only tokenizes the query.
const INDEX = POIS.map((p) => {
  const n = words(p.n);
  const a = words(p.a);
  return { p, n, a, na: [...n, ...a] };
});

const matches = (tokens: string[], ws: string[]) =>
  tokens.every((t) => ws.some((w) => w.startsWith(t)));

// Every query token must prefix-match a word in the POI. Rank by where the
// tokens land: name-prefix, name, address (exact house number before a
// prefix-only one, so "128" outranks "1280"), then split across both.
function score(entry: { n: string[]; a: string[]; na: string[] }, tokens: string[]): number {
  if (matches(tokens, entry.n)) {
    return entry.n[0]?.startsWith(tokens[0]) ? 0 : 1;
  }
  if (matches(tokens, entry.a)) {
    return entry.a.includes(tokens[0]) ? 2 : 3;
  }
  if (matches(tokens, entry.na)) return 4;
  return Infinity;
}

export const GET: APIRoute = ({ url }) => {
  const q = (url.searchParams.get('q') ?? '').trim().toLowerCase();
  if (q.length < 2) {
    return new Response(JSON.stringify({ error: 'Query must be at least 2 characters.' }), {
      status: 400,
      headers: { 'content-type': 'application/json' },
    });
  }

  const tokens = queryTokens(q);
  if (tokens.length === 0) {
    return new Response(JSON.stringify({ results: [] }), {
      headers: { 'content-type': 'application/json', 'cache-control': 'public, max-age=3600' },
    });
  }

  const scored: { s: number; p: Poi }[] = [];
  for (const entry of INDEX) {
    const s = score(entry, tokens);
    if (s !== Infinity) scored.push({ s, p: entry.p });
  }
  scored.sort((x, y) => x.s - y.s || x.p.n.localeCompare(y.p.n));

  const results = scored.slice(0, TOP_N).map(({ p }) => ({
    name: p.n,
    address: p.a,
    lat: p.lat,
    lon: p.lon,
  }));

  return new Response(JSON.stringify({ results }), {
    headers: { 'content-type': 'application/json', 'cache-control': 'public, max-age=3600' },
  });
};
