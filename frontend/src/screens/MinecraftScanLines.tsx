// The player card's live-scan lines (§M25; DESIGN "The Tricorder's live scan on the
// Minecraft map"), shared by the full map's card and Dave's `minecraft_map` chat card.
import { RadarIcon } from "../components/icons";
import type { ScanWords } from "../minecraftMap";

/** The held Tricorder's line under a radar glyph, then the depth a top-down map can't
 *  show, then the reach and when it last looked. */
export function ScanLines({ scan }: { scan: ScanWords }) {
  return (
    <p className="mc-map-det-tri mc-map-det-scan">
      <RadarIcon size={16} />
      <span>
        <b>Tricorder</b> · {scan.line}
        {scan.depth && <span className="mc-map-det-depth">{scan.depth}</span>}
        {scan.sub && <span className="mc-map-det-sub">{scan.sub}</span>}
      </span>
    </p>
  );
}
