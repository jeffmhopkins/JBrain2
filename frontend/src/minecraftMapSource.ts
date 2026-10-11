// Where the map page reads from. The owner's map asks the owner routes for any slot; a
// shared map asks the public /api/mc-share/{token} routes, where the token (not the slot the
// page passes) picks the world, and there is no structure re-scan to offer.

import {
  type MinecraftDim,
  type MinecraftMapInfo,
  type MinecraftMapPlayers,
  type MinecraftMapPoint,
  type MinecraftMapStructures,
  type MinecraftSweep,
  type MinecraftTimeline,
  type MinecraftTimelinePart,
  api,
  mcShareTileUrl,
  minecraftTileUrl,
} from "./api/client";
import type { BaseLayer } from "./minecraftMap";

export interface MapSource {
  info(slot: string, dim: MinecraftDim): Promise<MinecraftMapInfo>;
  point(slot: string, dim: MinecraftDim, x: number, z: number): Promise<MinecraftMapPoint>;
  players(slot: string): Promise<MinecraftMapPlayers>;
  structures(slot: string, dim: MinecraftDim): Promise<MinecraftMapStructures>;
  timeline(
    slot: string,
    from: number,
    to: number,
    opts?: { include?: MinecraftTimelinePart[]; players?: string[]; dim?: MinecraftDim },
  ): Promise<MinecraftTimeline>;
  tileUrl(
    slot: string,
    dim: MinecraftDim,
    zoom: number,
    tx: number,
    tz: number,
    layer: BaseLayer,
  ): string;
  /** Search the loaded world for structures again: the owner's only. */
  sweep: ((slot: string) => Promise<MinecraftSweep>) | null;
}

export const OWNER_MAP: MapSource = {
  info: (slot, dim) => api.minecraftMapInfo(slot, dim),
  point: (slot, dim, x, z) => api.minecraftMapPoint(slot, dim, x, z),
  players: (slot) => api.minecraftMapPlayers(slot),
  structures: (slot, dim) => api.minecraftMapStructures(slot, dim),
  timeline: (slot, from, to, opts) => api.minecraftTimeline(slot, from, to, opts),
  tileUrl: minecraftTileUrl,
  sweep: (slot) => api.minecraftMapSweep(slot),
};

export function shareMap(token: string): MapSource {
  return {
    info: (_slot, dim) => api.mcShareMapInfo(token, dim),
    point: (_slot, dim, x, z) => api.mcShareMapPoint(token, dim, x, z),
    players: () => api.mcShareMapPlayers(token),
    structures: (_slot, dim) => api.mcShareMapStructures(token, dim),
    timeline: (_slot, from, to, opts) => api.mcShareTimeline(token, from, to, opts),
    tileUrl: (_slot, dim, zoom, tx, tz, layer) => mcShareTileUrl(token, dim, zoom, tx, tz, layer),
    sweep: null,
  };
}

const SHARE_PATH = /^\/mc\/s\/([A-Za-z0-9_-]{1,128})\/?$/;

/** The public link for a map share token. */
export function mcShareUrl(token: string): string {
  return `${window.location.origin}/mc/s/${encodeURIComponent(token)}`;
}

/** The share token if `pathname` is a map-share path (/mc/s/{token}), else null. */
export function parseMcSharePath(pathname: string = window.location.pathname): string | null {
  const match = SHARE_PATH.exec(pathname);
  return match ? (match[1] as string) : null;
}
