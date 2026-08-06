import { Map as MapLibreMap, setWorkerUrl, type GeoJSONSource } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
// maplibre v6 resolves its module worker with a DYNAMIC new URL(...,
// import.meta.url), which Vite cannot statically analyze: dev pre-bundling
// relocates the module and the build emits no worker chunk, so the worker
// 404s and the map hangs silently (style loads, zero tiles, zero errors).
// `?worker&url` makes Vite bundle the worker self-contained (it imports
// maplibre-gl-shared.mjs) and hand back a real URL for both dev and prod.
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import { useEffect, useRef } from 'react';
import cellsJson from '../../data/cells.json';
import type { BandDef, PlaceValue, RippleResult } from '../../lib/models/types';

setWorkerUrl(maplibreWorkerUrl);

/* Positron: CARTO's light basemap, matching the Isos light surfaces.
   Swap this constant to move off the hosted basemap later. */
const STYLE_URL = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json';

const VENUE: [number, number] = [cellsJson.meta.venue.lon, cellsJson.meta.venue.lat];
const { dlat, dlon } = cellsJson.meta;

interface Props {
  bands: BandDef[];
  result: RippleResult | null;
  place: PlaceValue | null;
  focusCellId: string | null;
  rampMaxPct: number;
  /** which model arm painted the choropleth; null hides the tag (single-arm) */
  armLabel?: string | null;
  onPickPlace(lat: number, lon: number): void;
}

/* Choropleth ramp, derived from ONE configured ceiling.

   The previous stops (0/5/25/120/420) were taken from the simulator's core BAND
   lift of 410% but interpolate over CELL lift, whose simulator maximum is 108.9%
   and whose live maximum is 88.2%. The top two stops were unreachable and the
   map was a near-uniform wash. Deriving every stop as a fraction of one number
   keeps them all in range and makes retuning a one-line change.

   Fixed, not per-response quantiles. Normalizing each date to its own maximum
   would render a 25,000-attendance Tuesday identically to a 40,000 Saturday,
   which destroys the one thing the map is for. The live distribution is heavily
   skewed (p50 2.0%, p95 4.2%, max 88.2%), so the low stops sit close together to
   keep the ordinary blocks distinguishable. */
function fillPaint(maxPct: number) {
  const s = (f: number) => Math.max(0.01, Number((maxPct * f).toFixed(2)));
  return {
    'fill-color': [
      'interpolate', ['linear'], ['get', 'lift'],
      0, '#c7d3da',
      s(0.02), '#f9a19e',
      s(0.1), '#ed4037',
      s(0.35), '#b72025',
      s(1), '#77161e',
    ],
    'fill-opacity': [
      'interpolate', ['linear'], ['get', 'lift'],
      0, 0.08,
      s(0.005), 0.16,
      s(0.02), 0.3,
      s(0.1), 0.45,
      s(0.35), 0.6,
      s(1), 0.75,
    ],
  } as never;
}

/** Grid cell polygons, lift stamped into properties for data-driven paint. */
function cellsGeojson(liftById: Map<string, number>): GeoJSON.FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: cellsJson.cells.map((c) => ({
      type: 'Feature',
      properties: { id: c.id, lift: liftById.get(c.id) ?? 0 },
      geometry: {
        type: 'Polygon',
        coordinates: [[
          [c.gj * dlon, c.gi * dlat],
          [(c.gj + 1) * dlon, c.gi * dlat],
          [(c.gj + 1) * dlon, (c.gi + 1) * dlat],
          [c.gj * dlon, (c.gi + 1) * dlat],
          [c.gj * dlon, c.gi * dlat],
        ]],
      },
    })),
  };
}

/** 96-point circle around the venue at radius meters. */
function ring(radiusM: number): GeoJSON.Feature {
  const [lon0, lat0] = VENUE;
  const kLat = 111320;
  const kLon = 111320 * Math.cos((lat0 * Math.PI) / 180);
  const pts: [number, number][] = [];
  for (let i = 0; i <= 96; i++) {
    const a = (i / 96) * 2 * Math.PI;
    pts.push([lon0 + (radiusM * Math.cos(a)) / kLon, lat0 + (radiusM * Math.sin(a)) / kLat]);
  }
  return {
    type: 'Feature',
    properties: { radiusM },
    geometry: { type: 'LineString', coordinates: pts },
  };
}

function pinGeojson(place: PlaceValue | null): GeoJSON.FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: place
      ? [{
          type: 'Feature',
          properties: {},
          geometry: { type: 'Point', coordinates: [place.lon, place.lat] },
        }]
      : [],
  };
}

export default function ImpactMap({
  bands,
  result,
  place,
  focusCellId,
  rampMaxPct,
  armLabel,
  onPickPlace,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const readyRef = useRef(false);
  // Latest values survive the load-callback closure.
  const resultRef = useRef<RippleResult | null>(result);
  resultRef.current = result;
  const placeRef = useRef<PlaceValue | null>(place);
  placeRef.current = place;
  const focusRef = useRef<string | null>(focusCellId);
  focusRef.current = focusCellId;
  const pickRef = useRef(onPickPlace);
  pickRef.current = onPickPlace;

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new MapLibreMap({
      container: containerRef.current,
      style: STYLE_URL,
      center: VENUE,
      zoom: 11.8,
      cooperativeGestures: true,
      attributionControl: { compact: true },
    });
    mapRef.current = map;
    if (import.meta.env.DEV) {
      (window as unknown as { __map?: MapLibreMap }).__map = map;
    }

    map.on('load', () => {
      map.addSource('cells', { type: 'geojson', data: cellsGeojson(new Map()) });
      map.addLayer({
        id: 'cells-fill',
        type: 'fill',
        source: 'cells',
        paint: fillPaint(rampMaxPct),
      });
      map.addLayer({
        id: 'focus-line',
        type: 'line',
        source: 'cells',
        filter: ['==', ['get', 'id'], ''],
        paint: { 'line-color': '#b72025', 'line-width': 2.5 },
      });
      map.addSource('rings', {
        type: 'geojson',
        data: {
          type: 'FeatureCollection',
          // one circle per canonical ring edge (250m/500m/1km/2.5km/5km)
          features: bands.map((b) => ring(b.outerM)),
        },
      });
      map.addLayer({
        id: 'rings-line',
        type: 'line',
        source: 'rings',
        paint: { 'line-color': '#47535d', 'line-opacity': 0.4, 'line-width': 1 },
      });
      map.addSource('venue', {
        type: 'geojson',
        data: { type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: VENUE } },
      });
      map.addLayer({
        id: 'venue-dot',
        type: 'circle',
        source: 'venue',
        paint: {
          'circle-radius': 5,
          'circle-color': '#b72025',
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 2.5,
        },
      });
      map.addSource('pin', { type: 'geojson', data: pinGeojson(placeRef.current) });
      map.addLayer({
        id: 'pin-dot',
        type: 'circle',
        source: 'pin',
        paint: {
          'circle-radius': 7,
          'circle-color': '#343941',
          'circle-stroke-color': '#b72025',
          'circle-stroke-width': 2.5,
        },
      });
      readyRef.current = true;
      if (resultRef.current) applyResult(map, resultRef.current, rampMaxPct);
      applyFocus(map, focusRef.current);
    });

    map.on('click', (e) => {
      pickRef.current(e.lngLat.lat, e.lngLat.lng);
    });
    map.getCanvas().style.cursor = 'crosshair';

    const ro = new ResizeObserver(() => map.resize());
    ro.observe(containerRef.current);
    return () => {
      ro.disconnect();
      map.remove();
      mapRef.current = null;
      readyRef.current = false;
    };
    // The map mounts once; bands are static config.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current) return;
    if (result) applyResult(map, result, rampMaxPct);
    else {
      /* Clear to the zero-lift base rather than no-op. With a model toggle, a
         null result while the previous arm's choropleth stays painted would
         show one model's map under another model's label. */
      (map.getSource('cells') as GeoJSONSource | undefined)?.setData(
        cellsGeojson(new Map())
      );
    }
  }, [result]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current) return;
    (map.getSource('pin') as GeoJSONSource | undefined)?.setData(pinGeojson(place));
  }, [place]);

  useEffect(() => {
    const map = mapRef.current;
    if (map && readyRef.current) applyFocus(map, focusCellId);
  }, [focusCellId]);

  return (
    <div className="relative overflow-hidden rounded-3xl border border-mist bg-surface shadow-[0_4px_35px_rgba(68,83,94,0.15)]">
      <div ref={containerRef} className="h-[420px] w-full sm:h-[520px] lg:h-[600px]" />
      <div className="pointer-events-none absolute left-3 top-3 rounded-full border border-mist bg-surface/90 px-3 py-1 text-[11px] font-medium text-muted backdrop-blur">
        {place ? 'Rings at 250m, 500m, 1km, 2.5km, 5km around Oracle Park' : 'Click your spot, or search on the left'}
      </div>
      {/* The map has never had a legend. With a configurable ceiling the shading
          is not self-explanatory at all, so it needs one. */}
      {result ? (
        <div className="pointer-events-none absolute bottom-8 right-3 rounded-xl border border-mist bg-surface/90 px-3 py-2 backdrop-blur">
          <p className="mb-1 text-[10px] font-medium text-faint">
            {/* with a toggle, an unlabeled map is ambiguous about which model
                painted it */}
            Lift on this date{armLabel ? ` · ${armLabel}` : ''}
          </p>
          <div className="flex items-center gap-1">
            {LEGEND_STOPS.map((f) => (
              <span key={f} className="flex flex-col items-center gap-0.5">
                <span
                  className="h-3 w-6 rounded-sm"
                  style={{ backgroundColor: legendColor(f), opacity: legendOpacity(f) }}
                />
                <span className="text-[9px] tabular-nums text-faint">
                  {f === 0 ? '0' : `${Math.round(rampMaxPct * f)}%`}
                </span>
              </span>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

const LEGEND_STOPS = [0, 0.02, 0.1, 0.35, 1];
const LEGEND_COLORS = ['#c7d3da', '#f9a19e', '#ed4037', '#b72025', '#77161e'];
const LEGEND_OPACITY = [0.18, 0.3, 0.45, 0.6, 0.75];
const legendColor = (f: number) => LEGEND_COLORS[LEGEND_STOPS.indexOf(f)];
const legendOpacity = (f: number) => LEGEND_OPACITY[LEGEND_STOPS.indexOf(f)];

function applyResult(map: MapLibreMap, result: RippleResult, maxPct: number) {
  const liftById = new Map(result.cells.map((c) => [c.id, c.liftPct]));

  /* Defense in depth. The adapter throws an EndpointContractError on any cell-id
     mismatch, so this should be unreachable; it exists because the failure it
     guards against is invisible. cellsGeojson defaults an unknown id to 0, so a
     drifted grid would paint a flat map that looks exactly like a no-game day. */
  if (import.meta.env.DEV) {
    const covered = cellsJson.cells.filter((c) => liftById.has(c.id)).length;
    if (result.cells.length && covered < cellsJson.cells.length * 0.9) {
      console.error(
        `[ImpactMap] only ${covered}/${cellsJson.cells.length} cells matched the ` +
          'result; the map is rendering mostly zeros'
      );
    }
    const max = Math.max(0, ...result.cells.map((c) => c.liftPct));
    if (max > maxPct * 1.25) {
      console.warn(
        `[ImpactMap] cell lift ${max.toFixed(1)}% exceeds rampMaxPct ${maxPct}; ` +
          'the top of the ramp is saturating, retune config.rampMaxPct'
      );
    }
  }
  (map.getSource('cells') as GeoJSONSource | undefined)?.setData(cellsGeojson(liftById));
}

function applyFocus(map: MapLibreMap, focusCellId: string | null) {
  if (map.getLayer('focus-line')) {
    map.setFilter('focus-line', ['==', ['get', 'id'], focusCellId ?? '']);
  }
}
