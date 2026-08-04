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

// Pre-lowered copies so each request only lowercases the query.
const INDEX = POIS.map((p) => ({ p, n: p.n.toLowerCase(), a: p.a.toLowerCase() }));

function score(entry: { n: string; a: string }, q: string): number {
  if (entry.n.startsWith(q)) return 0;
  if (entry.n.includes(' ' + q)) return 1;
  if (entry.n.includes(q)) return 2;
  if (entry.a.startsWith(q)) return 3;
  if (entry.a.includes(q)) return 4;
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

  const scored: { s: number; p: Poi }[] = [];
  for (const entry of INDEX) {
    const s = score(entry, q);
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
