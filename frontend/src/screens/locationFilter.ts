// Side-effect-free location helpers (no Leaflet import) so the map glue and tests
// can share them.

import type { LocationFix } from "../api/client";

// The trail draws only reasonably-accurate fixes: a fix whose accuracy radius is
// wider than this is jittery indoor GPS that smears the path into a star-burst.
// Matches the backend geofence gate (locations/geofence.py `ACCURACY_GATE_M`) so the
// drawn trail and crossing detection agree on which fixes are trustworthy.
export const ACCURACY_GATE_M = 100;

/** Keep fixes within the accuracy gate; a null accuracy (unknown) is kept rather
 * than assumed bad. Order-preserving, so the trail still reads oldest → newest. */
export function withinAccuracy(
  fixes: LocationFix[],
  gateM: number = ACCURACY_GATE_M,
): LocationFix[] {
  return fixes.filter((f) => f.accuracy_m == null || f.accuracy_m <= gateM);
}
