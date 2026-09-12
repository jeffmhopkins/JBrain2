// Which kind Record is set to keep, remembered on this device.
//
// **Device-local, like the band picks and the theme**, and deliberately not a setting on
// the box: it is a property of how this phone is being used right now, it changes with a
// long press rather than through Settings, and one device preferring captions must not
// silently rearm the other's Record button. Every read and write is best-effort for the
// same reason `sdrBandPicks.ts` is — private mode, cleared site data and a browser that
// refuses storage all have to end in a working Record button, which is the one control
// on this surface that must never fail to render.

import type { SdrRecordKind } from "./api/client";

export const RECORD_KIND_KEY = "jb.sdr.recordKind";

/** What Record will keep. Audio unless this device has been swapped to captions — the
 *  default the api itself takes, so a lost preference records what it always did. */
export function loadRecordKind(): SdrRecordKind {
  try {
    return localStorage.getItem(RECORD_KIND_KEY) === "captions" ? "captions" : "audio";
  } catch {
    return "audio";
  }
}

/** Remember the swap, and hand back what was stored so the caller can draw from it
 *  without reading the key again — a write that failed must not leave the button
 *  showing a mode the next tap would not actually record. */
export function saveRecordKind(kind: SdrRecordKind): SdrRecordKind {
  try {
    localStorage.setItem(RECORD_KIND_KEY, kind);
  } catch {
    // best-effort; the swap still holds for this session, it just will not outlive it
  }
  return kind;
}
