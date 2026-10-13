// Where the map page reads from. The owner's map asks the owner routes for any slot; a
// shared map asks the public /api/mc-share/{token} routes, where the token (not the slot the
// page passes) picks the world, and there is no structure re-scan to offer.

import {
  type MinecraftDim,
  type MinecraftGoalProgress,
  type MinecraftMapEntities,
  type MinecraftMapInfo,
  type MinecraftMapMarkers,
  type MinecraftMapPlaces,
  type MinecraftMapPlayers,
  type MinecraftMapPoint,
  type MinecraftMapStructures,
  type MinecraftSweep,
  type MinecraftTimeline,
  type MinecraftTimelinePart,
  type MinecraftVillagerCard,
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
  /** The world's beacons and gates (§M26): the owner's only — a shared map has neither. */
  markers: ((slot: string) => Promise<MinecraftMapMarkers>) | null;
  /** Every player's saved places, the villagers and pets, a villager's trades, and a
   *  player's goal counts near their places (minecraft-map-layers): the owner's only. */
  places: ((slot: string) => Promise<MinecraftMapPlaces>) | null;
  entities: ((slot: string, dim: MinecraftDim) => Promise<MinecraftMapEntities>) | null;
  villager: ((slot: string, id: string) => Promise<MinecraftVillagerCard>) | null;
  goals: ((xuid: string) => Promise<MinecraftGoalProgress>) | null;
}

export const OWNER_MAP: MapSource = {
  info: (slot, dim) => api.minecraftMapInfo(slot, dim),
  point: (slot, dim, x, z) => api.minecraftMapPoint(slot, dim, x, z),
  players: (slot) => api.minecraftMapPlayers(slot),
  structures: (slot, dim) => api.minecraftMapStructures(slot, dim),
  timeline: (slot, from, to, opts) => api.minecraftTimeline(slot, from, to, opts),
  tileUrl: minecraftTileUrl,
  sweep: (slot) => api.minecraftMapSweep(slot),
  markers: (slot) => api.minecraftMapMarkers(slot),
  places: (slot) => api.minecraftMapPlaces(slot),
  entities: (slot, dim) => api.minecraftMapEntities(slot, dim),
  villager: (slot, id) => api.minecraftMapVillager(slot, id),
  goals: (xuid) => api.minecraftGoalProgress(xuid),
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
    markers: null,
    places: null,
    entities: null,
    villager: null,
    goals: null,
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
