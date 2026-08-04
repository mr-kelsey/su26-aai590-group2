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
  onPickPlace(lat: number, lon: number): void;
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

export default function ImpactMap({ bands, result, place, focusCellId, onPickPlace }: Props) {
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
        paint: {
          // mist -> brand red ramp over lift pct, on the light basemap
          'fill-color': [
            'interpolate', ['linear'], ['get', 'lift'],
            0, '#c7d3da', 5, '#f9a19e', 25, '#ed4037', 120, '#b72025', 420, '#77161e',
          ],
          'fill-opacity': [
            'interpolate', ['linear'], ['get', 'lift'],
            0, 0.08, 1, 0.16, 5, 0.3, 25, 0.45, 120, 0.6, 420, 0.75,
          ],
        },
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
      if (resultRef.current) applyResult(map, resultRef.current);
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
    if (map && readyRef.current && result) applyResult(map, result);
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
    </div>
  );
}

function applyResult(map: MapLibreMap, result: RippleResult) {
  const liftById = new Map(result.cells.map((c) => [c.id, c.liftPct]));
  (map.getSource('cells') as GeoJSONSource | undefined)?.setData(cellsGeojson(liftById));
}

function applyFocus(map: MapLibreMap, focusCellId: string | null) {
  if (map.getLayer('focus-line')) {
    map.setFilter('focus-line', ['==', ['get', 'id'], focusCellId ?? '']);
  }
}
