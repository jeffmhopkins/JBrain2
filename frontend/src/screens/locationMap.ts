// Pure geometry for the self-rendered schematic location map — no third-party
// tiles ever leave the box (L1). An equirectangular projection around the data's
// centre, fit to the viewport with a *single* metres-per-pixel scale so a fence
// circle stays a circle and a trail's distances stay comparable. The map is
// deliberately schematic (a relative layout), not a real basemap.

import type { LatLon } from "../api/client";

export interface Px {
  x: number;
  y: number;
}

const M_PER_DEG_LAT = 111_320;
// A floor on the projected span (metres) so a single fix — or a tight cluster —
// doesn't blow the scale up to absurd zoom; ~200 m reads as a sane default.
const MIN_SPAN_M = 200;

export interface Projector {
  project: (p: LatLon) => Px;
  /** Metres per pixel — converts a fence radius in metres to a pixel radius. */
  metersPerPixel: number;
}

export function buildProjector(
  points: LatLon[],
  width: number,
  height: number,
  pad = 0.12,
): Projector {
  const lats = points.map((p) => p.lat);
  const lons = points.map((p) => p.lon);
  const centerLat = points.length ? (Math.min(...lats) + Math.max(...lats)) / 2 : 0;
  const centerLon = points.length ? (Math.min(...lons) + Math.max(...lons)) / 2 : 0;
  const mPerLon = M_PER_DEG_LAT * Math.cos((centerLat * Math.PI) / 180);

  const toMetres = (p: LatLon): Px => ({
    x: (p.lon - centerLon) * mPerLon,
    y: (p.lat - centerLat) * M_PER_DEG_LAT, // north-positive metres
  });

  const metres = points.map(toMetres);
  const spanX = metres.length ? Math.max(...metres.map((m) => Math.abs(m.x))) * 2 : 0;
  const spanY = metres.length ? Math.max(...metres.map((m) => Math.abs(m.y))) * 2 : 0;
  const usableW = width * (1 - 2 * pad);
  const usableH = height * (1 - 2 * pad);
  const metersPerPixel = Math.max(
    spanX / usableW,
    spanY / usableH,
    MIN_SPAN_M / Math.min(usableW, usableH),
  );

  const project = (p: LatLon): Px => {
    const m = toMetres(p);
    return {
      x: width / 2 + m.x / metersPerPixel,
      y: height / 2 - m.y / metersPerPixel, // flip y: north is up
    };
  };
  return { project, metersPerPixel };
}

export interface HeatCell {
  x: number;
  y: number;
  size: number;
  /** 0..1 dwell density relative to the busiest cell. */
  weight: number;
}

// Bin projected points into a square grid; weight each cell by its share of the
// busiest cell. A coarse grid reads as a dwell heatmap without a real KDE.
export function heatCells(points: Px[], cell: number): HeatCell[] {
  const counts = new Map<string, number>();
  for (const p of points) {
    const key = `${Math.floor(p.x / cell)},${Math.floor(p.y / cell)}`;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  if (counts.size === 0) return [];
  const max = Math.max(...counts.values());
  return [...counts].map(([key, n]) => {
    const parts = key.split(",");
    return {
      x: Number(parts[0]) * cell,
      y: Number(parts[1]) * cell,
      size: cell,
      weight: n / max,
    };
  });
}
