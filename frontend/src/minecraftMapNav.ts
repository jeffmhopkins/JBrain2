// How a chat's map card opens the full Minecraft map (DESIGN "`minecraft_map` tool-view").
// The card renders deep in a chat transcript, far from the shell that owns the cards, so the
// shell registers how to get there — the engine banner's shape (engineState.ts).

import type { MinecraftDim } from "./api/client";

/** Where the full map opens: the card's world slot, dimension, centre and zoom, and
 *  whether to mark the centre (the thing the card was about). */
export interface MapIntent {
  slot: string;
  dim: MinecraftDim;
  x: number;
  z: number;
  /** The tile route's zoom: 0 is one pixel per block, 4 one per chunk, below 0 closer. */
  zoom: number;
  mark: boolean;
}

let opener: ((intent: MapIntent) => void) | null = null;

/** The shell registers how to push the map page; null when it unmounts. */
export function setMapOpener(fn: ((intent: MapIntent) => void) | null): void {
  opener = fn;
}

/** Open the full map, or false when nothing is registered to open it. */
export function openMinecraftMap(intent: MapIntent): boolean {
  if (!opener) return false;
  opener(intent);
  return true;
}
